"""Native MUBI Android TV service.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlparse

from ...core.cdm import CdmError
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from ...downloader.utils import pretty_codec
from . import api

_LOG = logging.getLogger(__name__)

_MANIFEST_RESOLUTION = Setting(
    key="manifest_resolution",
    label="Manifest resolution",
    kind="choice",
    options=[
        Option("best", "Automatic"),
        Option("2160", "2160p / 4K"),
        Option("1440", "1440p"),
        Option("1080", "1080p / FHD"),
        Option("720", "720p / HD"),
        Option("480", "480p / SD"),
    ],
    default="best",
    help=(
        "Selects the MUBI secure_url height before the manifest is opened. The "
        "shared Video quality setting still chooses tracks inside that manifest."
    ),
)

_MANIFEST_CODEC = Setting(
    key="manifest_codec",
    label="Manifest codec",
    kind="choice",
    options=[
        Option("any", "Automatic"),
        Option("av1", "AV1"),
        Option("vp9", "VP9"),
        Option("h265", "H.265 / HEVC"),
        Option("h264", "H.264"),
    ],
    default="any",
    help=(
        "Selects the codec family advertised in MUBI's viewing and secure_url "
        "requests. The shared Video codec setting still filters tracks afterward."
    ),
)


@registry.register
class Mubi(Service):
    ID = "mubi"
    NAME = "MUBI"
    TAG = "MUBI"
    ALIASES = ("mubi-tv",)
    TITLE_RE = r"(?:www\.)?mubi\.com/(?:[^/?#]+/)*(?:films|series)/"
    GEOFENCE = ()
    DESCRIPTION = "MUBI Android TV: TV-code login, films, series, search and Widevine."

    USES = Capabilities()
    SETTINGS = [_MANIFEST_RESOLUTION, _MANIFEST_CODEC]
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = False
    SUPPORTS_LIBRARY = False

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.MubiApi | None = None

    def _state(self) -> api.SessionState:
        return api.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))

    def _save_state(self, state: api.SessionState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def auth_status(self) -> AuthStatus:
        state = self._state()
        if state.signed_in:
            label = state.user_name or (f"user {state.user_id}" if state.user_id else "TV token")
            return AuthStatus(True, label, detail=state.country_code or "MUBI TV")
        return AuthStatus(False, "TV device is not paired", detail="Sign in with mubi.com/tv")

    def _new_client(self) -> api.MubiApi:
        return api.MubiApi(
            self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            state=self._state(),
            on_update=self._save_state,
        )

    def client(self, ctx: FlowContext | None = None) -> api.MubiApi:
        if self._api is None:
            self._api = self._new_client()
        if ctx is not None:
            ctx.status("Validating the MUBI TV session")
        try:
            self._api.ensure()
        except api.MubiAuthError:
            state = self._api.state
            state.token = ""
            state.user_id = ""
            state.user_name = ""
            self._save_state(state)
            raise
        if ctx is not None:
            label = self._api.state.user_name or self._api.state.user_id
            ctx.log(f"MUBI TV session ready{f' · {label}' if label else ''}", "ok")
        return self._api

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        try:
            ctx.status("Requesting a MUBI TV code")
            activation = client.start_login()
            state = yield ctx.wait_for(
                "Sign in to MUBI",
                [
                    ("Open", activation.url),
                    ("Code", activation.code),
                    "Confirm the code in the browser; this screen will continue automatically.",
                ],
                poll=lambda: client.poll_login(activation),
                timeout=float(activation.expires_in),
                interval=activation.poll_interval,
                hint="The code is linked to this MUBI TV device.",
            )
        except Back:
            return
        except api.MubiError as exc:
            ctx.error(str(exc))
            return
        if not isinstance(state, api.SessionState) or not state.signed_in:
            ctx.error("MUBI sign-in completed without a usable access token")
            return
        self._save_state(state)
        self._api = client
        label = state.user_name or state.user_id
        ctx.log(f"MUBI TV signed in{f' · {label}' if label else ''}", "ok")

    def logout(self) -> None:
        state = self._state()
        try:
            client = self._api or self._new_client()
            client.logout()
        except api.MubiError:
            _LOG.warning("MUBI server sign-out failed; clearing the local session")
        state.token = ""
        state.user_id = ""
        state.user_name = ""
        self._save_state(state)
        self._api = None
        super().logout()

    def prepare_drm(self, playback: Playback, tracks, log: Callable[[str], None]) -> None:
        self.client().verify_playback(playback.title.id)

    def prepare_download(self, playback: Playback, log: Callable[[str], None]) -> None:
        client = self.client()
        client.verify_playback(playback.title.id)
        selected = [reference for reference in playback.subtitle_references if reference.selected]
        if not selected:
            return
        directory = self.ctx.subtitle_dir()
        directory.mkdir(parents=True, exist_ok=True)
        for index, reference in enumerate(selected, 1):
            track = api.SubtitleTrack(
                f"sidecar-{index}", reference.url, reference.language, reference.kind, False,
            )
            subtitle = client.subtitle(track)
            digest = hashlib.sha256(reference.url.encode("utf-8") + b"\0" + subtitle.content).hexdigest()[:12]
            stem = self._safe_stem(
                f"{playback.save_name}.{reference.language}.{reference.kind}.{digest}"
            )
            path = directory / f"{stem}.{subtitle.suffix}"
            if not path.exists() or path.read_bytes() != subtitle.content:
                path.write_bytes(subtitle.content)
                os.chmod(path, 0o600)
            if not any(imported.path == str(path) for imported in playback.mux_imports):
                playback.mux_imports.append(ExternalTrack(
                    str(path), reference.language, reference.name, kind=reference.kind,
                    original=reference.original, forced=reference.kind == "forced",
                ))
            log(f"MUBI subtitle staged · {reference.name}")

    def _source_request(self) -> api.SourceRequest:
        return api.source_request(
            str(self.settings.get("manifest_resolution") or "best"),
            str(self.settings.get("manifest_codec") or "any"),
        )

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error("Expected a MUBI film or series URL")
            return
        try:
            client = self.client(ctx)
            if parsed.kind == "series":
                ctx.status(f"Loading MUBI series {parsed.slug}")
                yield from self._browse_series(ctx, client, client.series(parsed.slug))
                return
            ctx.status(f"Loading MUBI film {parsed.slug}")
            yield from self._emit_film(ctx, client, client.film(parsed.slug))
        except api.MubiError as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        query = str(query or "").strip()
        if not query:
            return
        try:
            client = self.client(ctx)
            ctx.status(f"Searching MUBI for {query}")
            hits = client.search(query)
        except api.MubiError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"MUBI found no results for {query}")
            return

        while True:
            try:
                hit = yield ctx.pick(
                    f"MUBI · {query}",
                    [
                        Choice(
                            item.label,
                            item,
                            detail=(item.film.synopsis[:120] if item.film else ""),
                            tags=(item.kind.upper(),),
                        )
                        for item in hits
                    ],
                )
            except Back:
                return
            if not isinstance(hit, api.SearchHit):
                return
            try:
                if hit.kind == "series":
                    ctx.status(f"Loading MUBI series {hit.series_title}")
                    yield from self._browse_series(ctx, client, client.series(hit.series_slug))
                elif hit.film is not None:
                    yield from self._emit_film(ctx, client, hit.film)
            except api.MubiError as exc:
                ctx.error(str(exc))

    def _browse_series(
        self,
        ctx: FlowContext,
        client: api.MubiApi,
        series: api.Series,
    ) -> Iterator[Ask]:
        if not series.seasons:
            ctx.warn(f"{series.title} has no available seasons")
            return
        while True:
            try:
                season = yield ctx.pick(
                    f"{series.title} · seasons",
                    [
                        Choice(
                            item.label,
                            item,
                            detail=f"{item.episode_count} episode(s)"
                            + (f" · {item.release_year}" if item.release_year else ""),
                        )
                        for item in series.seasons
                    ],
                )
            except Back:
                return
            if not isinstance(season, api.Season):
                return
            try:
                ctx.status(f"Loading {series.title} · season {season.number}")
                episodes = client.season_episodes(series.slug, season.id)
            except api.MubiError as exc:
                ctx.error(str(exc))
                continue
            if not episodes:
                ctx.warn(f"{series.title} season {season.number} has no playable episodes")
                continue

            while True:
                try:
                    picked = yield ctx.pick(
                        f"{series.title} · season {season.number}",
                        [
                            Choice(
                                item.episode.label,
                                item,
                                detail=item.synopsis[:120],
                                tags=("EPISODE",),
                            )
                            for item in episodes
                            if item.episode is not None
                        ],
                        multi=True,
                        hint="space to tick · enter to confirm",
                    )
                except Back:
                    break
                selected = [item for item in (picked or []) if isinstance(item, api.Film)]
                if not selected:
                    continue
                if len(selected) > 1:
                    ctx.batch(len(selected))
                for film in selected:
                    try:
                        yield from self._emit_film(ctx, client, film)
                    except api.MubiError as exc:
                        ctx.error(f"{film.label}: {exc}")
                break

    def _emit_film(
        self,
        ctx: FlowContext,
        client: api.MubiApi,
        film: api.Film,
    ) -> Iterator[Ask]:
        ctx.status(f"Authorizing {film.label}")
        source = client.playback(film.id, self._source_request())
        title = self._title(film)
        playback = self._playback(ctx, client, title, film, source)
        self._api = client
        ctx.log(source.note(), "ok")
        yield ctx.emit(playback)

    def _title(self, film: api.Film) -> Title:
        if film.episode is not None:
            return Title(
                id=film.id,
                kind=TitleKind.EPISODE,
                name=film.episode.series_title,
                season=film.episode.season,
                episode=film.episode.number,
                episode_name=film.episode.title,
                synopsis=film.synopsis or None,
                duration=film.duration_seconds,
                language=api.language_code(film.original_audio_language) or None,
                service=self.ID,
                data={"film": film},
            )
        return Title(
            id=film.id,
            kind=TitleKind.MOVIE,
            name=film.title,
            year=str(film.year) if film.year else None,
            synopsis=film.synopsis or None,
            duration=film.duration_seconds,
            language=api.language_code(film.original_audio_language) or None,
            service=self.ID,
            data={"film": film},
        )

    def _playback(
        self,
        ctx: FlowContext | None,
        client: api.MubiApi,
        title: Title,
        film: api.Film,
        source: api.Source,
    ) -> Playback:
        chapters = ()
        if self.fetch_chapters_enabled() and source.reel_id is not None:
            try:
                chapters = client.chapters(film.id, source.reel_id)
            except api.MubiError:
                message = "MUBI chapter metadata is unavailable for the active reel"
                if ctx is not None:
                    ctx.warn(message)
                else:
                    _LOG.warning(message)
        title.data["mubi_source_request"] = source.request
        drm = DrmInfo(clear=True)
        if source.encrypted:
            if not client.state.user_id or not client.state.token:
                raise api.MubiError("MUBI playback is missing its Widevine user/session context")
            drm = DrmInfo(
                system="widevine",
                license_url=api.LICENSE_URL,
                context={
                    "user_id": client.state.user_id,
                    "token": client.state.token,
                },
            )
        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            drm=drm,
            subtitle_references=self._subtitle_references(source.subtitles),
            chapters=list(chapters),
            attachments=list(film.attachments) if self.fetch_attachments_enabled() else [],
            strict_track_selection=True,
            requested=source.request.label,
            returned=source.video_codec.upper(),
            note=source.note(),
        )

    def filter_tracks(self, playback, tracks, log: Callable[[str], None]) -> list[StreamInfo] | None:
        original = playback.title.language or ""
        for stream in tracks.selectable_streams:
            if stream.media_type != "audio" or not api.original_audio_matches(stream.language or "", original):
                continue
            role = str(stream.role or "").strip()
            if not re.search(r"(?:^|\W)original(?:$|\W)", role, flags=re.IGNORECASE):
                stream.role = f"{role} Original".strip()
            stream.extra["original_language"] = original
        request = playback.title.data.get("mubi_source_request")
        film = playback.title.data.get("film")
        if not isinstance(request, api.SourceRequest):
            return None
        videos = [stream for stream in tracks.streams if stream.media_type == "video"]
        codecs = {
            str(pretty_codec(stream.codecs, "video") or "unknown").replace(".", "").upper()
            for stream in videos
        }
        if request.video_codec != "any" and not codecs.intersection(request.codecs):
            raise api.MubiError(
                f"MUBI requested {request.codecs_header} but the video ladder contains "
                f"{', '.join(sorted(codecs)) or 'no video codec'}"
            )
        dimensions = []
        for stream in videos:
            match = re.fullmatch(r"(\d+)[xX](\d+)", str(stream.resolution or ""))
            if match:
                dimensions.append(tuple(int(value) for value in match.groups()))
        # Scope films can be 3840x1608/1920: classify the native raster, never
        # upsample it or treat thumbnail/image representations as video.
        highest = max((max(height, width * 9 // 16) for width, height in dimensions), default=0)
        if request.height is not None and highest < request.height:
            actual = max(dimensions, key=lambda item: item[0] * item[1]) if dimensions else None
            label = f"{actual[0]}x{actual[1]}" if actual else "no identifiable video resolution"
            raise api.MubiError(
                f"MUBI requested {request.height}p but the returned video ladder has at most {label}; "
                "the requested manifest resolution is unavailable for this viewing"
            )
        if isinstance(film, api.Film) and "4K" in film.media_features and highest < 2160:
            log("MUBI catalogue advertises 4K; this viewing's manifest contains no 4K video")
        return None

    def get_playback(self, title: Title) -> Playback:
        film = title.data.get("film")
        if not isinstance(film, api.Film):
            raise api.MubiError("MUBI title is missing its film catalogue record")
        client = self.client()
        source = client.playback(film.id, self._source_request())
        return self._playback(None, client, title, film, source)

    @staticmethod
    def _subtitle_references(tracks: tuple[api.SubtitleTrack, ...]) -> list[SubtitleReference]:
        references: list[SubtitleReference] = []
        seen: set[tuple[str, str, str]] = set()
        for track in tracks:
            kind = {"subtitle": "normal", "forced-subtitle": "forced", "caption": "sdh"}.get(
                track.role, track.role or "normal"
            )
            key = (track.url, track.language, kind)
            if key in seen:
                continue
            seen.add(key)
            suffix = Path(urlparse(track.url).path).suffix.lower().lstrip(".")
            references.append(SubtitleReference(
                url=track.url, language=track.language, kind=kind, name=track.label,
                format=suffix if suffix in {"vtt", "srt", "ttml", "xml"} else "",
                headers={"User-Agent": api.USER_AGENT},
            ))
        return references

    def augment_tracks(self, playback: Playback, tracks, log: Callable[[str], None]) -> list[StreamInfo]:
        return [StreamInfo(
            manifest_type="service", media_type="subtitle",
            url=reference.url, original_url=reference.url,
            id=f"mubi-subtitle-{index}", group_id=reference.kind,
            name=reference.name, language=reference.language,
            role={"normal": None, "forced": "Forced", "sdh": "SDH"}.get(reference.kind, reference.kind),
            codecs={"vtt": "webvtt", "srt": "subrip", "ttml": "ttml", "xml": "xml"}.get(reference.format),
            extension=reference.format or None,
            extra={
                "service_sidecar": "subtitle", "subtitle_reference_url": reference.url,
                "subtitle_kind": reference.kind,
            },
        ) for index, reference in enumerate(playback.subtitle_references, 1)]

    @staticmethod
    def _safe_stem(value: str) -> str:
        stem = re.sub(r"[^\w.-]+", ".", str(value or ""), flags=re.UNICODE)
        return re.sub(r"\.{2,}", ".", stem).strip(".")[:180] or "MUBI"

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        context = dict(drm.context or {})
        client = self._api or self._new_client()
        try:
            return client.widevine_license(
                challenge,
                user_id=str(context.get("user_id") or ""),
                token=str(context.get("token") or ""),
            )
        except api.MubiError as exc:
            raise CdmError(str(exc)) from exc


__all__ = ["Mubi", "api"]

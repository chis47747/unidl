"""Tubi - free ad-supported streaming."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path

from ...core.credentials import mask_account
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from ...core.secureio import atomic_write_bytes, private_directory
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from . import api

_SUBTITLE_CODECS = {"srt": "subrip", "ttml": "ttml", "dfxp": "ttml", "xml": "ttml"}


def _subtitle_extension(url: str) -> str:
    clean = str(url or "").lower().split("?", 1)[0]
    for candidate in (".srt", ".vtt", ".ttml", ".dfxp", ".xml"):
        if clean.endswith(candidate):
            return candidate[1:]
    return "vtt"


_LOGIN_METHOD = Setting(
    key="login_method",
    label="Login method",
    kind="choice",
    options=[
        Option("anonymous", "Anonymous"),
        Option("tv", "TV code"),
    ],
    default="anonymous",
    help=(
        "Anonymous mints a signed Android TV device token. TV-code mode shows a "
        "code to enter at tubitv.com/activate and links this device to a Tubi "
        "account."
    ),
    resets_session=True,
)

_STREAM = Setting(
    key="stream",
    label="Stream type",
    kind="choice",
    options=[
        Option("dash_h264", "DASH H264"),
        Option("dash_h265", "DASH H265"),
        Option("hls", "HLS"),
    ],
    default="dash_h265",
    help="DASH H264 and DASH H265 are separate manifests on Tubi. HLS is the "
    "fallback when a DASH title will not parse.",
    resets_session=False,
)


class Tubi(Service):
    ID = "tubi"
    NAME = "Tubi"
    TAG = "TUBI"
    ALIASES = ("tubitv",)
    TITLE_RE = r"tubitv\.com/(?:movies|series|tv-shows)/"
    GEOFENCE = ("US",)
    DESCRIPTION = "Free, ad-supported."
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine", "playready")
    SETTINGS = [_LOGIN_METHOD, _STREAM]
    CREDENTIALS = []
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = "tubi_token.json"

    def login_method(self) -> str:
        value = str(self.settings.get("login_method") or "anonymous").strip().lower()
        return value if value in {"anonymous", "tv"} else "anonymous"

    @staticmethod
    def _account_label(token: api.Token) -> str:
        if token.name:
            return token.name
        if token.email:
            return mask_account(token.email)
        return "Tubi account"

    def auth_status(self) -> AuthStatus:
        token = api.Token.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if token.account and token.valid:
            return AuthStatus(
                logged_in=True,
                anonymous_ok=True,
                label=self._account_label(token),
                detail=f"token · {token.hours_left}h left",
            )
        if token.account and token.refreshable:
            return AuthStatus(
                logged_in=True,
                anonymous_ok=True,
                label=self._account_label(token),
                detail="token expired · refreshes on use",
            )
        if token.valid:
            return AuthStatus(False, "anonymous device token", f"{token.hours_left}h left", True)
        if token.refreshable:
            return AuthStatus(False, "anonymous token ready to refresh", anonymous_ok=True)
        if self.login_method() == "tv":
            return AuthStatus(False, "not paired", "anonymous browsing remains available", True)
        return AuthStatus(False, "no sign-in needed", anonymous_ok=True)

    def stream_type(self) -> str:
        return api.normalize_stream(self.settings.get("stream"))

    def _new_client(self) -> api.TubiApi:
        return api.TubiApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            stream=self.stream_type(),
        )

    def _save(self, client: api.TubiApi) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, client.to_cache())

    def _ensure_playback(self, client: api.TubiApi) -> None:
        had = client.playback.valid
        client.ensure_playback_token()
        if client.playback.valid and not had:
            self._save(client)

    def client(self) -> api.TubiApi:
        client = self._new_client()
        adopted = client.adopt(self.ctx.tokens.read(self.TOKEN_FILE))
        if adopted:
            self._ensure_playback(client)
            return client
        if client.token.refreshable:
            try:
                client.refresh()
            except api.TubiError:
                pass
            else:
                self._save(client)
                self._ensure_playback(client)
                return client
        client.anonymous_token(client.device_id)
        self._save(client)
        self._ensure_playback(client)
        return client

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        client.adopt(self.ctx.tokens.read(self.TOKEN_FILE))
        if self.login_method() == "anonymous":
            try:
                ctx.status("Starting a Tubi anonymous session")
                if not client.token.valid:
                    if client.token.refreshable:
                        try:
                            client.refresh()
                        except api.TubiError:
                            client.anonymous_token(client.device_id)
                    else:
                        client.anonymous_token(client.device_id)
            except api.TubiError as exc:
                ctx.error(str(exc))
                return
            self._save(client)
            ctx.log("Tubi anonymous session ready", "ok")
            return

        if client.token.account and client.token.valid:
            ctx.log(f"already signed in as {self._account_label(client.token)}", "ok")
            return
        if client.token.account and client.token.refreshable:
            try:
                ctx.status("Refreshing the Tubi account token")
                client.refresh()
            except api.TubiError:
                pass
            else:
                self._save(client)
                ctx.log(f"signed in as {self._account_label(client.token)}", "ok")
                return
        try:
            ctx.status("Getting a Tubi device token")
            if not client.token.valid:
                if client.token.refreshable:
                    try:
                        client.refresh()
                    except api.TubiError:
                        client.anonymous_token(client.device_id)
                else:
                    client.anonymous_token(client.device_id)
        except api.TubiError as exc:
            ctx.error(str(exc))
            return
        self._save(client)
        try:
            ctx.status("Requesting a Tubi TV code")
            pairing = client.start_pairing()
        except api.TubiError as exc:
            ctx.error(str(exc))
            return

        def poll() -> api.Token | None:
            return client.poll_pairing(pairing)

        try:
            token = yield ctx.wait_for(
                "Pair Tubi on another device",
                [
                    ("Code", pairing.code),
                    ("Open", api.PAIRING_URL),
                ],
                poll=poll,
                timeout=api.PAIRING_TIMEOUT,
                interval=api.PAIRING_INTERVAL,
                hint="Open the page, sign in, enter the TV code, then return here.",
            )
        except (api.TubiError, Back) as exc:
            if isinstance(exc, api.TubiError):
                ctx.error(str(exc))
            return
        if not isinstance(token, api.Token) or not token.account:
            ctx.error("Tubi pairing completed without a usable account session")
            return
        self._save(client)
        ctx.log(f"signed in as {self._account_label(client.token)}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._licence_api = None
        super().logout()

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(f"That does not look like a Tubi link or id: {target}")
            return

        ctx.status("Getting a device token")
        try:
            client = self.client()
        except api.TubiError as exc:
            ctx.error(str(exc))
            return

        if parsed.kind == "series":
            yield from self._series(ctx, client, parsed.value)
        else:
            yield from self._movie(ctx, client, parsed.value)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client()
            ctx.status(f"Searching Tubi for {query}")
            results = client.search(query)
        except api.TubiError as exc:
            ctx.error(str(exc))
            return
        if not results:
            return
        while True:
            try:
                chosen = yield ctx.pick(
                    f"Results for {query}",
                    [
                        Choice(
                            item.label,
                            item,
                            detail=f"{item.kind}  ·  {item.synopsis[:70]}".strip(),
                        )
                        for item in results
                    ],
                )
            except Back:
                return
            if not isinstance(chosen, api.Item):
                return
            try:
                if chosen.kind == "series":
                    yield from self._series(ctx, client, chosen.id)
                elif chosen.kind == "channel":
                    yield from self._emit(ctx, client, chosen, live=True)
                else:
                    yield from self._movie(ctx, client, chosen.id)
            except Back:
                continue
            except api.TubiError as exc:
                ctx.error(str(exc))
                continue
            if not ctx.interactive:
                return

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client()
            ctx.status("Loading Live TV categories")
            groups = client.channel_groups()
        except api.TubiError as exc:
            ctx.error(str(exc))
            return
        if not groups:
            ctx.warn("Tubi returned no Live TV categories")
            return
        if len(groups) == 1:
            yield from self._live_channels(ctx, client, groups[0])
            return
        while True:
            try:
                group = yield ctx.pick(
                    "Live TV categories",
                    [Choice(entry.name, entry, detail=f"{entry.count} channel(s)") for entry in groups],
                )
            except Back:
                return
            if not isinstance(group, api.ChannelGroup):
                return
            try:
                yield from self._live_channels(ctx, client, group)
            except Back:
                continue
            except api.TubiError as exc:
                ctx.error(str(exc))
                continue
            if not ctx.interactive:
                return

    def _live_channels(
        self, ctx: FlowContext, client: api.TubiApi, group: api.ChannelGroup
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading {group.name}")
            channels = client.channels(group)
        except api.TubiError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn(f"Tubi returned no channels for {group.name}")
            return
        while True:
            try:
                chosen = yield ctx.table(
                    group.name,
                    ["Channel", "On now"],
                    [(item.name, item.synopsis) for item in channels],
                    channels,
                )
            except Back:
                return
            if not isinstance(chosen, api.Item):
                return
            try:
                yield from self._emit(ctx, client, chosen, live=True)
            except Back:
                continue
            except api.TubiError as exc:
                ctx.error(str(exc))
                continue
            if not ctx.interactive:
                return

    def _movie(self, ctx: FlowContext, client: api.TubiApi, content_id: str) -> Iterator[Ask]:
        try:
            item, _data = client.movie(content_id)
        except api.TubiError as exc:
            ctx.error(str(exc))
            return
        yield from self._emit(ctx, client, item)

    def _series(self, ctx: FlowContext, client: api.TubiApi, series_id: str) -> Iterator[Ask]:
        try:
            series, episodes = client.episodes(series_id)
        except api.TubiError as exc:
            ctx.error(str(exc))
            return
        ctx.log(f"{series}: {len(episodes)} episode(s)", "ok")
        if not episodes:
            ctx.warn(f"Tubi lists no episodes for {series}")
            return
        seasons: dict[str, list[api.Item]] = {}
        for item in episodes:
            seasons.setdefault(item.season or "0", []).append(item)
        if len(seasons) == 1:
            selected_name, selected = next(iter(seasons.items()))
            yield from self._season_episodes(ctx, client, series, selected_name, selected)
            return
        while True:
            try:
                season_choice = yield ctx.pick(
                    series,
                    [
                        Choice(f"Season {season}", (season, values), detail=f"{len(values)} episode(s)")
                        for season, values in seasons.items()
                    ],
                )
            except Back:
                return
            if season_choice is None:
                return
            selected_name, selected = season_choice
            try:
                yield from self._season_episodes(ctx, client, series, selected_name, selected)
            except Back:
                continue
            except api.TubiError as exc:
                ctx.error(str(exc))
                continue
            if not ctx.interactive:
                return

    def _season_episodes(
        self,
        ctx: FlowContext,
        client: api.TubiApi,
        series: str,
        selected_name: str,
        selected: list[api.Item],
    ) -> Iterator[Ask]:
        while True:
            try:
                picked = yield ctx.pick(
                    f"{series} · Season {selected_name}",
                    [Choice(item.label, item, detail=item.synopsis[:80]) for item in selected],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                return
            chosen = [item for item in (picked or []) if isinstance(item, api.Item)]
            if not chosen:
                return
            if len(chosen) > 1:
                try:
                    ctx.batch(len(chosen))
                except Back:
                    continue
            for item in chosen:
                try:
                    yield from self._emit(ctx, client, item)
                except Back:
                    break
                except api.TubiError as exc:
                    ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _emit(self, ctx: FlowContext, client: api.TubiApi, item: api.Item, *, live: bool = False) -> Iterator[Ask]:
        ctx.status(f"Resolving {item.name}")
        live = live or item.kind == "channel"
        data = item.data
        if item.kind == "episode" or not data.get("video_resources"):
            try:
                data = client.content(item.id)
            except api.TubiError as exc:
                if not live:
                    ctx.error(f"{item.label}: {exc}")
                    return
        sources = client.sources(data)
        if not sources:
            ctx.warn(f"{item.label}: Tubi offered no playable rendition")
            return

        wanted = self.drm_system()
        source = self._choose(sources, wanted)
        for candidate in sources:
            ctx.log(f"  {candidate.line()}{'  <-' if candidate is source else ''}")

        if source.system and source.system != wanted:
            ctx.warn(
                f"{item.label}: only a {source.system} rendition is offered, so {wanted} was not used for this one"
            )
        requested_codec = self._requested_codec()
        if requested_codec != "any" and not self._codec_matches(source, requested_codec):
            ctx.warn(f"{item.label}: {requested_codec} is unavailable; using {source.codec or '?'}")
        requested_quality = str(self.settings.get("video_quality") or "best").casefold()
        if requested_quality.isdigit() and source.height != int(requested_quality):
            ctx.warn(f"{item.label}: {requested_quality}p is unavailable; using {source.height or '?'}p")

        kind = TitleKind.CHANNEL if live else (TitleKind.EPISODE if item.kind == "episode" else TitleKind.MOVIE)
        title = Title(
            id=item.id,
            kind=kind,
            name=item.series or item.name,
            episode_name=item.name if item.kind == "episode" else None,
            season=int(item.season) if item.season else None,
            episode=int(item.number) if item.number else None,
            year=item.year or None,
            channel=item.name if live else None,
            synopsis=item.synopsis or None,
            service=self.ID,
            data={"programs": item.data.get("programs") or []} if live else {},
        )
        save_name = self.save_name(title)
        references = self._subtitle_references(source, live=live)
        chapters = api.chapters_from(data) if self.fetch_chapters_enabled() and not live else []
        attachments = api.attachments_from(data) if self.fetch_attachments_enabled() else []
        playback = Playback(
            title=title,
            save_name=save_name,
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT, "Referer": f"{api.TV_UI_URL}/"},
            proxy=self.ctx.proxy,
            subtitle_references=references,
            chapters=chapters,
            attachments=attachments,
            is_live=live,
            note=f"{source.line()}  ·  {len(references)} subtitle track(s)",
        )
        if source.encrypted:
            playback.drm = DrmInfo(system=source.system, license_url=source.license_url)
        else:
            playback.drm = DrmInfo(clear=True)
        yield ctx.emit(playback)

    @staticmethod
    def _codec_matches(source: api.Source, wanted: str) -> bool:
        codec = source.codec.casefold().replace(".", "")
        return (
            (wanted == "h264" and ("h264" in codec or "avc" in codec))
            or (wanted == "h265" and ("h265" in codec or "hevc" in codec))
            or wanted in codec
        )

    def _requested_codec(self) -> str:
        pinned = api.STREAM_CODEC.get(self.stream_type())
        if pinned:
            return pinned
        return str(self.settings.get("video_codec") or "any").casefold()

    def _choose(self, sources: list[api.Source], wanted: str) -> api.Source:
        """Apply DRM, codec and quality before choosing Tubi's fixed manifest."""
        matching = [source for source in sources if source.system == wanted]
        clear = [source for source in sources if not source.encrypted]
        candidates = matching or clear or sources

        codec = self._requested_codec()
        if codec != "any":
            codec_matches = [source for source in candidates if self._codec_matches(source, codec)]
            if codec_matches:
                candidates = codec_matches

        quality = str(self.settings.get("video_quality") or "best").casefold()
        if quality == "worst":
            return min(candidates, key=lambda source: source.height)
        if quality not in ("best", "") and quality.isdigit():
            target = int(quality)
            exact = [source for source in candidates if source.height == target]
            if exact:
                return exact[0]
            below = [source for source in candidates if 0 < source.height <= target]
            if below:
                return max(below, key=lambda source: source.height)
            known = [source for source in candidates if source.height > 0]
            if known:
                return min(known, key=lambda source: source.height)
        return candidates[0]

    @staticmethod
    def _subtitle_references(source: api.Source, *, live: bool) -> list[SubtitleReference]:
        if live:
            return []
        references: list[SubtitleReference] = []
        seen: set[str] = set()
        for track in source.subtitles:
            url = str(track.url or "").strip()
            if not url:
                continue
            key = url.split("?", 1)[0].casefold()
            if key in seen:
                continue
            seen.add(key)
            language = str(track.language or "und").strip() or "und"
            references.append(
                SubtitleReference(
                    url=url,
                    language=language,
                    name=language,
                    format=_subtitle_extension(url),
                )
            )
        return references

    def augment_tracks(self, playback: Playback, tracks, log):
        del tracks
        rows: list[StreamInfo] = []
        seen: set[str] = set()
        for index, reference in enumerate(playback.subtitle_references, 1):
            url = str(reference.url or "").strip()
            if not url:
                continue
            key = url.split("?", 1)[0].casefold()
            if key in seen:
                continue
            seen.add(key)
            extension = reference.format or _subtitle_extension(url)
            language = reference.language or "und"
            rows.append(
                StreamInfo(
                    manifest_type="service",
                    media_type="subtitle",
                    url=url,
                    original_url=url,
                    id=f"tubi-subtitle-{index}",
                    group_id=reference.kind or "normal",
                    name=reference.name or language,
                    language=language,
                    codecs=_SUBTITLE_CODECS.get(extension, "webvtt"),
                    extension=extension,
                    extra={
                        "service_sidecar": "subtitle",
                        "subtitle_reference_url": url,
                        "subtitle_kind": reference.kind or "normal",
                    },
                )
            )
        if rows:
            log(f"Tubi: added {len(rows)} API subtitle track(s) to the picker")
        return rows

    def prepare_download(self, playback: Playback, log) -> None:
        selected = [reference for reference in playback.subtitle_references if reference.selected]
        if not selected:
            return
        try:
            directory = private_directory(self.ctx.subtitle_dir())
        except OSError as exc:
            log(f"Could not create the Tubi subtitle directory: {exc}", "warning")
            return
        client = self.client()
        stem = re.sub(r"[^\w.-]+", ".", playback.save_name)
        stem = re.sub(r"\.{2,}", ".", stem).strip(".")[:180] or "Tubi"
        staged = {Path(item.path).resolve() for item in playback.mux_imports if item.path}
        prepared = 0
        for index, reference in enumerate(selected, 1):
            language = re.sub(r"[^a-zA-Z0-9_-]+", "", reference.language) or "und"
            try:
                data = client.subtitle(api.Subtitle(url=reference.url, language=reference.language or "und"))
            except api.TubiError as exc:
                log(f"subtitle skipped · {language}: {exc}", "warning")
                continue
            suffix = f".{reference.format or _subtitle_extension(reference.url)}"
            digest = hashlib.sha256(reference.url.encode("utf-8") + b"\0" + data).hexdigest()[:12]
            destination = directory / f"{stem}.{language}.{index}.{digest}{suffix}"
            try:
                atomic_write_bytes(destination, data)
            except OSError as exc:
                log(f"subtitle skipped · {language}: {exc}", "warning")
                continue
            resolved = destination.resolve()
            if resolved in staged:
                continue
            staged.add(resolved)
            playback.mux_imports.append(ExternalTrack(str(destination), language, reference.name or language))
            prepared += 1
        if prepared:
            log(f"Tubi subtitles: prepared {prepared}/{len(selected)} track(s)", "ok")

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        return self._licence_client().widevine_license(drm.license_url, challenge, drm.headers)

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        """PlayReady, against the same server, which speaks SOAP."""
        return self._licence_client().playready_license(drm.license_url, challenge, drm.headers)

    def _licence_client(self):
        client = getattr(self, "_licence_api", None)
        if client is None:
            client = self.client()
            self._licence_api = client
        return client


registry.register(Tubi)

__all__ = ["Tubi", "api"]

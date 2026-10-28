"""FilmRise - free ad-supported movies, TV shows, and live FAST channels.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path

from ...core.cdm import CdmError
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, ExternalTrack, Playback
from ...core.secureio import atomic_write_bytes, private_directory
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from . import api

_SUBTITLE_CODECS = {
    "srt": "subrip",
    "vtt": "webvtt",
    "ttml": "ttml",
    "dfxp": "ttml",
    "xml": "ttml",
}


def _subtitle_extension(url: str) -> str:
    clean = str(url or "").lower().split("?", 1)[0]
    for ext in (".srt", ".vtt", ".ttml", ".dfxp", ".xml"):
        if clean.endswith(ext):
            return ext[1:]
    return "srt"


@registry.register
class FilmRise(Service):
    ID = "filmrise"
    NAME = "FilmRise"
    TAG = "FILMRISE"
    ALIASES = ("filmrise", "film_rise", "filmrisetv")
    TITLE_RE = (
        r"(?:https?://)?(?:(?:www\.)?filmrise\.com)/"
        r"|\b(?:filmrise|film_rise|filmrisetv):"
    )
    GEOFENCE = ()
    DESCRIPTION = (
        "FilmRise movies, TV shows, and live FAST channels over the Android TV API: "
        "anonymous session, browse home, native search, Widevine DRM, attachments, and chapters."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.FilmRiseApi | None = None


    def auth_status(self) -> AuthStatus:
        raw = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        state = api.TokenState.from_cache(raw)
        if state.is_valid():
            return AuthStatus(
                logged_in=True,
                label="anonymous session",
                detail=f"Device: {state.device_id[:8]}...",
                anonymous_ok=True,
            )
        if state.is_refreshable():
            return AuthStatus(
                logged_in=True,
                label="session ready to refresh",
                detail="Anonymous session",
                anonymous_ok=True,
            )
        return AuthStatus(
            logged_in=False,
            label="anonymous · initialized on demand",
            detail="No sign-in required",
            anonymous_ok=True,
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        """Bootstrap or refresh an anonymous FilmRise device session."""
        ctx.status("Initializing FilmRise anonymous session")
        client = self._new_client()
        try:
            state = client.ensure_session()
            self._save_state(state)
            self._api = client
            ctx.log("FilmRise anonymous session active", "ok")
        except api.FilmRiseError as exc:
            ctx.error(f"Failed to initialize anonymous session: {exc}")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None

    def _save_state(self, state: api.TokenState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def _new_client(self) -> api.FilmRiseApi:
        state = api.TokenState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        return api.FilmRiseApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            state=state,
            on_update=self._save_state,
        )

    def client(self, ctx: FlowContext | None = None) -> api.FilmRiseApi:
        if self._api is not None:
            if self._api.state.is_valid():
                return self._api
            try:
                self._api.ensure_session()
                return self._api
            except api.FilmRiseError:
                self._api = None

        client = self._new_client()
        if ctx is not None:
            ctx.status("Opening FilmRise session")
        client.ensure_session()
        self._api = client
        return client


    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        while True:
            status = self._auth_status_quietly()
            options: list[Choice] = [
                Choice("Browse FilmRise home", "browse", detail="FilmRise home, TV, movies and live channels"),
                Choice("Search", "search", detail="Native FilmRise search"),
                Choice("Live TV", "live", detail="Live FAST channels"),
                Choice("VOD · open a URL or content ID", "url", detail="Content ID or reference URI"),
                Choice("Settings", "settings"),
            ]

            label = "Refresh anonymous session" if status.logged_in else "Initialize session"
            options.append(Choice(label, "login", detail=status.label))
            if status.logged_in or status.logout_available:
                options.append(Choice("Sign out", "logout", detail=status.label))

            action = yield ctx.pick("", options, scope=SCOPE_ROOT)

            try:
                if action == "browse":
                    yield from self.browse(ctx)
                elif action == "search":
                    yield from self._search_menu(ctx)
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "url":
                    yield from self._url_menu(ctx)
                elif action == "settings":
                    yield ctx.settings_request()
                elif action == "login":
                    yield from self.login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("FilmRise session cleared", "ok")
            except Back:
                continue

    def _search_menu(self, ctx: FlowContext) -> Iterator[Ask]:
        note: list[str] = []
        while True:
            query = yield ctx.text("Search FilmRise", lines=note)
            wanted = str(query or "").strip()
            if not wanted:
                break
            try:
                found = yield from self._did_anything(self.search(ctx, wanted))
            except Back:
                continue
            note = (
                []
                if found
                else [
                    f"Nothing found for {wanted!r}.",
                    f"{self.NAME} has no match for that - try fewer words.",
                ]
            )
            if not ctx.interactive:
                break

    def _url_menu(self, ctx: FlowContext) -> Iterator[Ask]:
        while True:
            target = yield ctx.text("Enter content ID or reference URI (e.g. 10758618 or filmrise:82992)")
            text = str(target or "").strip()
            if not text:
                break
            try:
                yield from self.open_url(ctx, text)
            except Back:
                continue
            if not ctx.interactive:
                break


    def browse(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        ctx.status("Loading FilmRise home sections")
        try:
            sections = client.get_sections()
        except api.FilmRiseError as exc:
            ctx.error(str(exc))
            return

        if not sections:
            ctx.warn("No FilmRise sections available")
            return

        while True:
            try:
                chosen_sec = yield ctx.pick(
                    "FilmRise · Browse",
                    [Choice(sec.title, sec, detail="Live channels" if sec.is_live else "Catalogue section") for sec in sections],
                )
            except Back:
                return

            if chosen_sec is None:
                return

            if chosen_sec.is_live:
                yield from self.live(ctx)
            else:
                yield from self._browse_shelves(ctx, client, chosen_sec)

    def _browse_shelves(
        self, ctx: FlowContext, client: api.FilmRiseApi, section: api.SectionItem
    ) -> Iterator[Ask]:
        ctx.status(f"Loading {section.title} shelves")
        try:
            shelves = client.get_shelves(section.title, section.url)
        except api.FilmRiseError as exc:
            ctx.error(str(exc))
            return

        if not shelves:
            ctx.warn(f"No shelves found in section {section.title}")
            return

        while True:
            try:
                chosen_shelf = yield ctx.pick(
                    f"FilmRise · {section.title} · {len(shelves)} categories",
                    [Choice(sh.title, sh, detail="Live channels" if sh.is_live else "Row") for sh in shelves],
                )
            except Back:
                return

            if chosen_shelf is None:
                return

            yield from self._browse_shelf_items(ctx, client, chosen_shelf)

    def _browse_shelf_items(
        self, ctx: FlowContext, client: api.FilmRiseApi, shelf: api.ShelfItem
    ) -> Iterator[Ask]:
        ctx.status(f"Loading titles for {shelf.title}")
        try:
            items = client.get_shelf_items(shelf.url)
        except api.FilmRiseError as exc:
            ctx.error(str(exc))
            return

        if not items:
            ctx.warn(f"No titles found in {shelf.title}")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    f"{shelf.title} · {len(items)} items",
                    [Choice(it.label, it, detail=it.detail) for it in items],
                )
            except Back:
                return

            if chosen is None:
                return

            if isinstance(chosen, api.LiveChannel):
                yield from self._emit_live(ctx, client, chosen)
            elif chosen.is_show:
                yield from self._open_show(ctx, client, chosen.id, series_item=chosen)
            else:
                yield from self._emit_playable(ctx, client, chosen)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        client = self.client(ctx)
        ctx.status(f"Searching FilmRise for {query}")
        try:
            hits = client.search(query)
        except api.FilmRiseError as exc:
            ctx.error(str(exc))
            return

        if not hits:
            ctx.warn(f"FilmRise found nothing for {query}")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    f"FilmRise · {query} · {len(hits)} results",
                    [Choice(hit.label, hit, detail=hit.detail) for hit in hits],
                )
            except Back:
                return

            if chosen is None:
                return

            if chosen.is_show:
                yield from self._open_show(ctx, client, chosen.id, series_item=chosen)
            else:
                yield from self._emit_playable(ctx, client, chosen)

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        ctx.status("Loading FilmRise live channels")
        try:
            channels = client.get_live_channels()
        except api.FilmRiseError as exc:
            ctx.error(str(exc))
            return

        if not channels:
            ctx.warn("No FilmRise live channels available")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    f"FilmRise Live · {len(channels)} channels",
                    [Choice(ch.label, ch, detail=ch.detail) for ch in channels],
                )
            except Back:
                return

            if chosen is None:
                return

            yield from self._emit_live(ctx, client, chosen)

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        try:
            kind, content_id = api.parse_filmrise_reference(target)
        except api.FilmRiseError as exc:
            ctx.error(str(exc))
            return

        slug_match = re.search(
            r"/(?:movies|movie|shows|tv-shows|fawesome-topics|watch)/[0-9]+(?:[-/]([^/?#]+))?",
            target,
            re.IGNORECASE,
        )
        title_hint = ""
        if slug_match and slug_match.group(1):
            title_hint = slug_match.group(1).replace("-", " ").strip()

        client = self.client(ctx)
        yield from self._open_reference(ctx, client, kind, content_id, title_hint=title_hint)


    def _open_reference(
        self,
        ctx: FlowContext,
        client: api.FilmRiseApi,
        kind: str,
        content_id: str,
        title_hint: str = "",
    ) -> Iterator[Ask]:
        trimmed = api.trim_id(content_id)

        if kind == "show":
            yield from self._open_show(ctx, client, trimmed)
            return

        if kind == "live":
            ctx.status(f"Loading live channel {trimmed}")
            channels = client.get_live_channels()
            ch = next((c for c in channels if api.trim_id(c.id) == trimmed), None)
            if ch:
                yield from self._emit_live(ctx, client, ch)
                return
            ctx.error(f"Live channel {content_id} not found")
            return

        if kind == "movie":
            ctx.status(f"Loading movie {trimmed}")
            try:
                item = client.get_video(trimmed, title_hint=title_hint)
                yield from self._emit_playable(ctx, client, item)
                return
            except api.FilmRiseError as exc:
                ctx.error(str(exc))
                return

        try:
            series_item, seasons = client.get_show(trimmed)
            if seasons:
                yield from self._open_show(ctx, client, trimmed, series_item=series_item)
                return
        except Exception:
            pass

        ctx.status(f"Loading title {trimmed}")
        try:
            item = client.get_video(trimmed, title_hint=title_hint)
            yield from self._emit_playable(ctx, client, item)
        except api.FilmRiseError as exc:
            ctx.error(str(exc))

    def _open_show(
        self,
        ctx: FlowContext,
        client: api.FilmRiseApi,
        show_id: str,
        series_item: api.TitleItem | None = None,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading TV show {show_id}")
        try:
            series, seasons = client.get_show(show_id)
        except api.FilmRiseError as exc:
            ctx.error(str(exc))
            return

        if not seasons:
            ctx.warn(f"No seasons found for show {series.title}")
            return

        if series_item and series_item.title:
            series = series_item

        if len(seasons) == 1:
            yield from self._pick_episodes(ctx, client, series, seasons[0])
            return

        while True:
            try:
                chosen_season = yield ctx.pick(
                    f"{series.title} · {len(seasons)} seasons",
                    [Choice(s.title, s, detail=s.description or f"Season {s.number}") for s in seasons],
                )
            except Back:
                return

            if chosen_season is None:
                return

            yield from self._pick_episodes(ctx, client, series, chosen_season)

    def _pick_episodes(
        self,
        ctx: FlowContext,
        client: api.FilmRiseApi,
        series: api.TitleItem,
        season: api.SeasonItem,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading episodes for {season.title}")
        try:
            episodes = client.get_season_episodes(season.feed_url)
        except api.FilmRiseError as exc:
            ctx.error(str(exc))
            return

        if not episodes:
            ctx.warn(f"No episodes found for {season.title}")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    f"{series.title} · {season.title} · {len(episodes)} episodes",
                    [Choice(ep.label, ep, detail=ep.detail) for ep in episodes],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                return

            if not chosen:
                return

            selected = list(chosen) if isinstance(chosen, (list, tuple)) else [chosen]
            if len(selected) > 1:
                ctx.batch(len(selected))

            for ep in selected:
                yield from self._emit_playable(ctx, client, ep, series=series)
            return

    def _emit_playable(
        self,
        ctx: FlowContext,
        client: api.FilmRiseApi,
        item: api.TitleItem,
        series: api.TitleItem | None = None,
    ) -> Iterator[Ask]:
        if not item.video_url:
            ctx.error(f"No playback URL found for {item.title}")
            return

        playback = self._playback(client, item, series=series)
        yield ctx.emit(playback)

    def _emit_live(
        self,
        ctx: FlowContext,
        client: api.FilmRiseApi,
        channel: api.LiveChannel,
    ) -> Iterator[Ask]:
        title = Title(
            id=channel.id,
            kind=TitleKind.PROGRAM,
            name=channel.title,
            service=self.ID,
        )
        clean_url = api.clean_live_macro_url(channel.stream_url)
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=clean_url,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=True,
            drm=DrmInfo(clear=True),
            note=channel.detail,
        )

        poster = channel.images.get("poster") or channel.images.get("thumbnail")
        if poster:
            from ...core.attachments import Attachment

            playback.attachments = [
                Attachment(
                    url=poster,
                    name="Channel Logo",
                    kind="artwork",
                    mime_type="image/jpeg" if poster.endswith((".jpg", ".jpeg")) else "image/png",
                )
            ]

        yield ctx.emit(playback)

    def _playback(
        self,
        client: api.FilmRiseApi,
        item: api.TitleItem,
        series: api.TitleItem | None = None,
    ) -> Playback:
        if item.kind == "episode":
            series_name = series.title if series else (item.series_name or item.title)
            ep_year = item.year or (series.year if series else None)
            title = Title(
                id=item.id,
                kind=TitleKind.EPISODE,
                name=series_name,
                year=str(ep_year) if ep_year else None,
                season=item.season_number,
                episode=item.episode_number,
                episode_name=item.episode_title or item.title,
                service=self.ID,
            )
        else:
            title = Title(
                id=item.id,
                kind=TitleKind.MOVIE,
                name=item.title,
                year=str(item.year) if item.year else None,
                service=self.ID,
            )

        if item.duration:
            title.duration = item.duration

        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=item.video_url,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=False,
            note=item.detail,
        )

        if item.is_drm:
            playback.drm = DrmInfo(
                system="widevine",
                license_url=client.get_drm_license_url(),
                headers={"User-Agent": api.USER_AGENT},
            )
        else:
            playback.drm = DrmInfo(clear=True)

        playback.subtitle_references = client.extract_subtitles(item)

        if self.fetch_chapters_enabled():
            chapters = client.parse_chapters(item)
            if chapters:
                playback.chapters = chapters

        if self.fetch_attachments_enabled():
            attachments = client.extract_attachments(item)
            if attachments:
                playback.attachments = attachments

        return playback

    def augment_tracks(
        self, playback: Playback, tracks, log
    ) -> list[StreamInfo] | None:
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
            codec = _SUBTITLE_CODECS.get(extension, "subrip" if extension == "srt" else "webvtt")
            language = reference.language or "und"
            name = reference.name or language
            rows.append(
                StreamInfo(
                    manifest_type="service",
                    media_type="subtitle",
                    url=url,
                    original_url=url,
                    id=f"filmrise-subtitle-{index}",
                    group_id=reference.kind or "normal",
                    name=name,
                    language=language,
                    codecs=codec,
                    extension=extension,
                    extra={
                        "service_sidecar": "subtitle",
                        "subtitle_reference_url": url,
                        "subtitle_kind": reference.kind or "normal",
                    },
                )
            )
        if rows:
            log(f"FilmRise: added {len(rows)} API subtitle track(s) to the picker")
        return rows

    def prepare_download(self, playback: Playback, log) -> None:
        selected = [subtitle for subtitle in playback.subtitle_references if subtitle.selected]
        if not selected:
            return
        client = self._api or self._new_client()
        try:
            directory = private_directory(self.ctx.subtitle_dir())
        except OSError as exc:
            log(f"Could not create FilmRise subtitle directory: {exc}")
            return
        staged = {Path(item.path).resolve() for item in playback.mux_imports if item.path}
        prepared = 0
        for index, subtitle in enumerate(selected, 1):
            try:
                data = client.fetch_subtitle(subtitle.url)
            except api.FilmRiseError as exc:
                log(f"could not download {subtitle.name or subtitle.language} subtitles: {exc}")
                continue
            extension = subtitle.format or _subtitle_extension(subtitle.url)
            digest = hashlib.sha256(subtitle.url.encode("utf-8") + b"\0" + data).hexdigest()[:12]
            language = subtitle.language or "und"
            stem = f"{playback.save_name}.{language}.{index}.{digest}"[:180] or "FilmRise"
            path = directory / f"{stem}.{extension}"
            try:
                atomic_write_bytes(path, data)
            except OSError as exc:
                log(f"could not write {subtitle.name or subtitle.language} subtitles: {exc}")
                continue
            resolved = path.resolve()
            if resolved in staged:
                continue
            staged.add(resolved)
            playback.mux_imports.append(
                ExternalTrack(
                    path=str(path),
                    language=language,
                    name=subtitle.name or language,
                    kind=subtitle.kind or "normal",
                    default=True if subtitle.selected and prepared == 0 else None,
                )
            )
            prepared += 1
        if prepared:
            log(f"FilmRise subtitles: prepared {prepared}/{len(selected)} track(s)")

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        client = self._api or self._new_client()
        try:
            return client.widevine_license(drm.license_url, challenge)
        except api.FilmRiseError as exc:
            raise CdmError(str(exc)) from exc


__all__ = ["FilmRise", "api"]

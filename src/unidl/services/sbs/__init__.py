"""SBS On Demand and SBS Audio - catch-up, podcasts, television and radio."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

from ...core.attachments import Attachment
from ...core.credentials import mask_account
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, ExternalTrack, Playback
from ...core.qr import QrPresentation
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


@registry.register
class Sbs(Service):
    ID = "sbs"
    NAME = "SBS On Demand"
    TAG = "SBS"
    ALIASES = ("sbsondemand", "sbsaudio", "sbsradio", "sbs.com.au")
    TITLE_RE = r"^(?:https?://)?(?:www\.)?sbs\.com\.au/"
    GEOFENCE = ("AU",)
    MEDIA_TYPES = ("audio", "video")
    DESCRIPTION = (
        "SBS On Demand, podcasts, radio catch-up and live radio. TV-code login; HLS playback "
        "as in the legacy script (no Widevine/PlayReady licence exchange)."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ()
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = api.TOKEN_FILE
    DEVICE_ID_FILE = api.DEVICE_ID_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.SbsApi | None = None
        self._audio: api.SbsAudioApi | None = None

    def auth_status(self) -> AuthStatus:
        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if not state.device_id:
            state.device_id = self._read_device_id()
        detail = "token cache"
        if state.token_valid():
            who = state.email or "signed in"
            return AuthStatus(True, who, detail=detail)
        if state.refresh_token:
            who = state.email or "signed in"
            return AuthStatus(True, f"{who} · refresh ready", detail=detail)
        return AuthStatus(
            False, "TV not linked", anonymous_ok=True,
            detail="SBS Audio available; choose Sign in for On Demand TV",
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        try:
            ctx.status("Starting SBS TV-code login")
            challenge = client.start_tv_link()

            def poll() -> api.Session | api.SbsError | None:
                try:
                    return client.poll_tv_link(challenge)
                except api.SbsError as exc:
                    # Await retries raised exceptions; terminal auth errors end it.
                    return exc

            state = yield ctx.wait_for(
                "Link SBS On Demand",
                [("Open", challenge.verification_url), ("Code", challenge.user_code)],
                poll=poll,
                timeout=challenge.remaining,
                interval=challenge.interval,
                qr=QrPresentation(
                    image_url=challenge.qr_image_url,
                    payload=challenge.verification_url,
                    fallback_url=challenge.verification_url,
                    alt="SBS On Demand TV login",
                ),
            )
            if isinstance(state, api.SbsError):
                raise state
            if not isinstance(state, api.Session) or not state.access_token:
                raise api.SbsError("SBS TV-code login finished without an account")
            self._save_state(state)
        except Back:
            return
        except (api.SbsError, OSError) as exc:
            ctx.error(str(exc))
            return
        self._api = client
        ctx.log(f"SBS TV linked · {mask_account(state.email) or 'signed in'}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None

    def _read_device_id(self) -> str:
        path = self.ctx.tokens.resolve(self.DEVICE_ID_FILE)
        if path is None:
            return ""
        try:
            return path.read_text("utf-8").strip()
        except OSError:
            return ""

    def _write_device_id(self, device_id: str) -> None:
        if not device_id:
            return
        path = self.ctx.tokens.path(self.DEVICE_ID_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(device_id.strip() + "\n", "utf-8")

    def _save_state(self, state: api.Session) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())
        self._write_device_id(state.device_id)

    def _new_client(self, state: api.Session | None = None) -> api.SbsApi:
        session_state = state or api.Session()
        if not session_state.device_id:
            session_state.device_id = self._read_device_id()
        return api.SbsApi(
            session=self.ctx.session(user_agent=api.TV_USER_AGENT, cookies=False),
            state=session_state,
            on_save=self._save_state,
        )

    def client(self, ctx: FlowContext | None = None) -> api.SbsApi:
        client = self._api
        if client is None:
            state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
            client = self._new_client(state)
        if client.state.token_valid():
            self._api = client
            return client

        if client.state.refresh_token:
            if ctx:
                ctx.status("Refreshing SBS session")
            if client.refresh():
                self._api = client
                if ctx:
                    ctx.log("SBS token refreshed", "ok")
                return client

        self._api = None
        raise api.SbsError("SBS is not signed in. Choose Sign in to pair using a TV code.")

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like an SBS On Demand link or media id: {target}\n"
                "Expected an SBS On Demand or SBS Audio link, or a bare mpx media id."
            )
            return
        if parsed.kind == "audio":
            yield from self._open_audio(ctx, parsed.path)
            return
        if parsed.kind == "collection":
            ctx.warn(
                "Collections are navigation containers and do not have one direct stream. "
                "Open an item inside the collection, or search for it."
            )
            return
        try:
            client = self.client(ctx)
        except (api.SbsError, OSError) as exc:
            ctx.error(str(exc))
            return
        try:
            if parsed.kind == "series":
                yield from self._series(ctx, client, parsed.path or target)
            elif parsed.kind == "episode":
                ctx.status("Loading episode")
                item = client.episode(parsed.entity or "tv-episode", parsed.value)
                yield from self._emit(ctx, client, item)
            else:
                ctx.status("Loading title")
                item = client.program(parsed.entity or "watch", parsed.value)
                yield from self._emit(ctx, client, item)
        except (api.SbsError, OSError) as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        page = ctx.pick("Search SBS", [
            Choice("Movies & TV", "video"), Choice("Podcasts & radio", "audio"),
        ])
        while True:
            mode = yield page
            if mode is None:
                return
            try:
                if mode == "audio":
                    yield from self._audio_listing(ctx, lambda number: self.audio_client().search(query, number))
                else:
                    yield from self._search_video(ctx, query)
            except Back:
                if not ctx.interactive:
                    raise
            if not ctx.interactive:
                return

    def _search_video(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status(f"Searching SBS for {query}")
            hits = client.search(query)
        except (api.SbsError, OSError) as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"SBS found nothing for {query}")
            return
        page = ctx.pick(
            f"SBS  ·  {query}",
            [
                Choice(
                    hit.title,
                    hit,
                    detail=(hit.description or hit.url)[:90],
                    tags=tuple(
                        value
                        for value in (
                            hit.entity_type.replace("_", " ").title(),
                            hit.mpx_media_id,
                        )
                        if value
                    ),
                )
                for hit in hits
            ],
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            page.cursor = hits.index(chosen)
            try:
                yield from self._open_hit(ctx, client, chosen)
            except Back:
                if not ctx.interactive:
                    raise
            if not ctx.interactive:
                return

    def _open_hit(
        self, ctx: FlowContext, client: api.SbsApi, hit: api.SearchHit
    ) -> Iterator[Ask]:
        if hit.is_collection:
            ctx.warn(
                "That result is a collection. Open an item on SBS, then paste its URL."
            )
            return
        try:
            if hit.is_series:
                path = ""
                if hit.url:
                    parsed = api.parse_input(hit.url)
                    path = (parsed.path if parsed else "") or ""
                if not path and hit.slug:
                    entity = hit.entity_type.lower().replace("_", "-")
                    path = f"/ondemand/{entity}/{hit.slug}"
                if not path:
                    ctx.warn(f"No series path for {hit.title}")
                    return
                yield from self._series(ctx, client, path)
                return
            if hit.mpx_media_id:
                ctx.status(f"Loading {hit.title}")
                as_episode = "EPISODE" in hit.entity_type
                item = client.mpx_media(hit.mpx_media_id, as_episode=as_episode)
                yield from self._emit(ctx, client, item)
                return
            if hit.url:
                yield from self.open_url(ctx, hit.url)
                return
            ctx.warn(f"Unsupported search result: {hit.entity_type}")
        except (api.SbsError, OSError) as exc:
            ctx.error(str(exc))

    def _series(self, ctx: FlowContext, client: api.SbsApi, path: str) -> Iterator[Ask]:
        try:
            ctx.status("Loading series")
            series = client.series(path)
        except (api.SbsError, OSError) as exc:
            ctx.error(str(exc))
            return
        if not series.seasons:
            ctx.warn(f"SBS returned no seasons for {series.title}")
            return
        if len(series.seasons) == 1:
            yield from self._episodes(ctx, client, series, series.seasons[0])
            return
        page = ctx.pick(
            f"{series.title}  ·  seasons",
            [Choice(entry.label, entry) for entry in series.seasons],
        )
        while True:
            season = yield page
            if season is None:
                return
            page.cursor = series.seasons.index(season)
            try:
                yield from self._episodes(ctx, client, series, season)
            except Back:
                if not ctx.interactive:
                    raise
            if not ctx.interactive:
                return

    def _episodes(
        self, ctx: FlowContext, client: api.SbsApi, series: api.Series, season: api.Season
    ) -> Iterator[Ask]:
        if not season.episodes:
            ctx.warn(f"SBS returned no episodes for {series.title}  ·  {season.label}")
            return
        page = ctx.pick(
            f"{series.title}  ·  {season.label}",
            [Choice(item.label, item) for item in season.episodes],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        while True:
            picked = yield page
            items = list(picked or [])
            if not items:
                return
            page.preselected = [season.episodes.index(item) for item in items]
            page.cursor = page.preselected[0]
            try:
                if len(items) > 1 and not ctx.interactive:
                    ctx.batch(len(items))
                for item in items:
                    yield from self._emit(ctx, client, item)
            except Back:
                if not ctx.interactive:
                    raise
            if not ctx.interactive:
                return

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        page = ctx.pick("SBS live", [Choice("TV", "video"), Choice("Radio", "audio")])
        while True:
            mode = yield page
            if mode is None:
                return
            try:
                if mode == "audio":
                    yield from self._audio_listing(ctx, lambda _number: self.audio_client().stations())
                else:
                    yield from self._live_video(ctx)
            except Back:
                if not ctx.interactive:
                    raise
            if not ctx.interactive:
                return

    def _live_video(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading SBS live channels")
            channels = client.channels()
        except (api.SbsError, OSError) as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("SBS returned no live channels")
            return
        page = ctx.table(
            f"SBS  ·  {len(channels)} live channels",
            ["Channel", "Code", "Media id"],
            [(channel.title, channel.code, channel.mpx_media_id) for channel in channels],
            channels,
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            try:
                yield from self._emit_live(ctx, client, chosen)
            except Back:
                if not ctx.interactive:
                    raise
            if not ctx.interactive:
                return

    def _emit_live(
        self, ctx: FlowContext, client: api.SbsApi, channel: api.Channel
    ) -> Iterator[Ask]:
        item = api.MediaItem(
            id=channel.mpx_media_id,
            name=channel.title,
            kind="live",
        )
        yield from self._emit(ctx, client, item, live=True)

    def audio_client(self) -> api.SbsAudioApi:
        if self._audio is None:
            self._audio = api.SbsAudioApi(
                self.ctx.session(user_agent=api.TV_USER_AGENT, cookies=False)
            )
        return self._audio

    def _open_audio(self, ctx: FlowContext, path: str) -> Iterator[Ask]:
        client = self.audio_client()
        try:
            ctx.status("Loading SBS Audio")
            first = client.page(path)
        except (api.SbsError, OSError) as exc:
            ctx.error(str(exc))
            return
        if len(first.items) == 1 and first.items[0].source and (
            "podcast-episode" in path.split("/") or first.items[0].kind == "radio-channel"
        ):
            try:
                playback = self._audio_playback(first.items[0])
            except api.SbsError as exc:
                ctx.error(str(exc))
                return
            yield ctx.emit(playback)
            return
        yield from self._audio_listing(ctx, lambda number: first if number == 1 else client.page(path, number))

    def _audio_listing(self, ctx: FlowContext, load) -> Iterator[Ask]:
        number = 1
        previous_number = 1
        pages = {}
        while True:
            if number not in pages:
                try:
                    ctx.status("Loading SBS Audio")
                    listing = load(number)
                except (api.SbsError, OSError) as exc:
                    ctx.error(str(exc))
                    if pages:
                        number = previous_number
                        continue
                    return
                choices = [Choice(
                    item.label, item, detail=(item.published or item.synopsis)[:90],
                    tags=(item.type_label,),
                ) for item in listing.items]
                if listing.items and all(item.source and item.kind != "radio-channel" for item in listing.items):
                    choices.append(Choice("Select multiple episodes", "batch"))
                if listing.next_page is not None:
                    choices.append(Choice("Next page", listing.next_page))
                if number > 1:
                    choices.append(Choice("Previous page", number - 1))
                if not choices:
                    ctx.warn("SBS Audio found no playable items")
                    return
                pages[number] = listing, ctx.pick(
                    f"{listing.title} · {number}" if listing.next_page or number > 1 else listing.title,
                    choices,
                )
            listing, page = pages[number]
            previous_number = number
            try:
                chosen = yield page
            except Back:
                if number > 1 and ctx.interactive:
                    number -= 1
                    continue
                raise
            if chosen is None:
                return
            if isinstance(chosen, int):
                number = chosen
                continue
            page.cursor = next((i for i, choice in enumerate(page.choices) if choice.value == chosen), 0)
            try:
                if chosen == "batch":
                    yield from self._audio_batch(ctx, listing)
                elif isinstance(chosen, api.AudioItem):
                    if chosen.source:
                        yield ctx.emit(self._audio_playback(chosen))
                    else:
                        yield from self._open_audio(ctx, chosen.url)
            except Back:
                if not ctx.interactive:
                    raise
            except (api.SbsError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _audio_batch(self, ctx: FlowContext, listing: api.AudioPage) -> Iterator[Ask]:
        page = ctx.pick(listing.title, [Choice(item.label, item) for item in listing.items], multi=True)
        while True:
            chosen = yield page
            if not chosen:
                return
            selected = chosen if isinstance(chosen, list) else [chosen]
            if len(selected) > 1:
                ctx.batch(len(selected))
            try:
                for item in selected:
                    yield ctx.emit(self._audio_playback(item))
            except Back:
                if not ctx.interactive:
                    raise
            if not ctx.interactive:
                return

    def _audio_playback(self, item: api.AudioItem) -> Playback:
        if not item.source or urlparse(item.source).scheme not in {"http", "https"}:
            raise api.SbsError(f"SBS Audio returned no playable source for {item.name}")
        live = item.kind == "radio-channel"
        cover = item.cover if not urlparse(item.cover).path.lower().endswith(".svg") else ""
        episode_name = item.name if item.programme and item.programme != item.name else None
        if item.kind == "radio-program" and item.published:
            episode_name = item.label
        title = Title(
            id=item.id, kind=TitleKind.STATION if live else TitleKind.TRACK,
            name=item.programme or item.name,
            episode_name=episode_name,
            year=item.published or None, season=item.season, episode=item.episode,
            track_number=item.episode, duration=item.duration, language=item.language or None,
            channel=item.name if live else None, artist=item.artist or item.programme or "SBS Audio",
            album=item.programme or None, publisher="SBS", genre="Radio" if live or item.kind == "radio-program" else "Podcast",
            synopsis=item.synopsis or None, cover_url=cover or None, service=self.ID,
        )
        headers = {"User-Agent": api.TV_USER_AGENT, "Accept": "*/*"}
        return Playback(
            title=title, save_name=self.save_name(title), manifest_url=item.source,
            headers=headers, proxy=self.ctx.proxy, drm=DrmInfo(clear=True),
            attachments=[Attachment(cover, name="Cover", kind="artwork", headers=headers)]
            if cover and self.fetch_attachments_enabled() else [],
            note="SBS Radio · live" if live else "SBS Audio · podcast / catch-up",
        )

    def _emit(
        self,
        ctx: FlowContext,
        client: api.SbsApi,
        item: api.MediaItem,
        *,
        live: bool = False,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {item.label}")
            source = client.source(
                item.id,
                fetch_chapters=not (live or item.kind == "live") and self.fetch_chapters_enabled(),
                fetch_attachments=self.fetch_attachments_enabled(),
            )
        except (api.SbsError, OSError) as exc:
            ctx.error(f"{item.label}: {exc}")
            return

        is_live = live or item.kind == "live" or source.is_live
        if is_live:
            kind = TitleKind.CHANNEL
            title = Title(
                id=item.id,
                kind=kind,
                name=item.name,
                channel=item.name,
                service=self.ID,
            )
        elif item.kind == "episode":
            title = Title(
                id=item.id,
                kind=TitleKind.EPISODE,
                name=item.series_title or item.name,
                episode_name=item.name,
                season=item.season,
                episode=item.episode,
                year=item.year or None,
                language=item.language or None,
                synopsis=item.synopsis or None,
                service=self.ID,
            )
        else:
            title = Title(
                id=item.id,
                kind=TitleKind.MOVIE,
                name=item.name,
                year=item.year or None,
                language=item.language or None,
                synopsis=item.synopsis or None,
                service=self.ID,
            )

        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={
                "User-Agent": api.TV_USER_AGENT,
                "Accept": "*/*",
            },
            proxy=self.ctx.proxy,
            is_live=is_live,
            drm=DrmInfo(clear=True),
            note=source.line(),
            chapters=list(source.chapters) if not is_live else [],
            attachments=list(source.attachments),
        )
        playback.mux_imports = self._stage_subtitles(ctx, client, source, playback.save_name)
        yield ctx.emit(playback)

    def _stage_subtitles(
        self,
        ctx: FlowContext,
        client: api.SbsApi,
        source: api.Source,
        save_name: str,
    ) -> list[ExternalTrack]:
        tracks: list[ExternalTrack] = []
        seen: dict[str, int] = {}
        for subtitle in source.subtitles:
            language = subtitle.lang.strip().replace("_", "-") or "und"
            safe_language = re.sub(r"[^A-Za-z0-9._-]+", "-", language).strip(".-") or "und"
            seen[safe_language] = seen.get(safe_language, 0) + 1
            duplicate = seen[safe_language]
            duplicate_suffix = f".{duplicate}" if duplicate > 1 else ""
            source_suffix = Path(urlparse(subtitle.url).path).suffix.lower()
            suffix = source_suffix if source_suffix in {".srt", ".ttml", ".vtt", ".xml"} else ".vtt"
            digest = hashlib.sha256(subtitle.url.encode("utf-8")).hexdigest()[:12]
            filename = f"{Path(save_name).name}.{safe_language}{duplicate_suffix}.{digest}{suffix}"
            try:
                path = self.ctx.subtitle_dir() / filename
                data = client.subtitle(subtitle.url)
                if not path.exists() or path.read_bytes() != data:
                    path.write_bytes(data)
                    os.chmod(path, 0o600)
            except (api.SbsError, OSError) as exc:
                ctx.warn(f"subtitle {language} skipped: {exc}")
                continue
            tracks.append(ExternalTrack(path=str(path), language=language, name=language))
        if tracks:
            ctx.log(f"prepared {len(tracks)} external subtitle track(s)", "ok")
        return tracks


__all__ = ["Sbs", "api"]

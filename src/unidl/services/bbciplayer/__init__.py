"""BBC iPlayer - programme catalogue, Sounds audio, search, live TV and radio."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext, Pick
from ...core.playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from ...core.qr import QrPresentation
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from . import api, sounds

_FLOW_ERRORS = (RuntimeError, requests.RequestException, OSError)


def _browse_page(
    ctx: FlowContext,
    page: Ask,
    visit: Callable[[Any], Iterator[Ask]],
) -> Iterator[Ask]:
    while True:
        try:
            selected = yield page
        except Back:
            return
        if selected is None or selected == []:
            return
        if isinstance(page, Pick):
            values = selected if page.multi and isinstance(selected, (list, tuple)) else [selected]
            indexes = [index for index, choice in enumerate(page.choices) if choice.value in values]
            if indexes:
                page.cursor = indexes[0]
                if page.multi:
                    page.preselected = [index for index in indexes if not page.choices[index].navigates]
        try:
            yield from visit(selected)
        except Back:
            pass
        except _FLOW_ERRORS as exc:
            ctx.error(str(exc))
        if not ctx.interactive:
            return


def _browse_input(
    ctx: FlowContext,
    title: str,
    visit: Callable[[FlowContext, str], Iterator[Ask]],
) -> Iterator[Ask]:
    page = ctx.text(title)
    while True:
        try:
            target = str((yield page) or "").strip()
        except Back:
            return
        if not target:
            return
        page.default = target
        try:
            yield from visit(ctx, target)
        except Back:
            pass
        except _FLOW_ERRORS as exc:
            ctx.error(str(exc))
        if not ctx.interactive:
            return


class _PlaybackSourceSetting(Setting):
    def coerce(self, value):
        return api.normalize_source_profiles(value)


_RESOLUTION = _PlaybackSourceSetting(
    "source_resolution",
    "Playback source",
    kind="multi",
    options=[
        Option("auto", "Best available · UHD → FHD → HD", exclusive=True),
        Option("uhd-dash", "UHD · 2160p DASH"),
        Option("uhd-hls", "UHD · 2160p HLS"),
        Option("fhd-dash", "FHD · 1080p DASH"),
        Option("fhd-hls", "FHD · 1080p HLS"),
        Option("hd-dash", "HD · 720p DASH"),
        Option("hd-hls", "HD · 720p HLS"),
    ],
    default=("auto",),
    help=(
        "BBC media-selector sources to request. Best available is exclusive: it "
        "asks for UHD, then FHD, then HD, and keeps the first source that works. "
        "Tick several DASH/HLS rows to authorize those VOD manifests and let UniDL "
        "merge them before track selection. Live TV and BBC Sounds always use one "
        "source so the playlist can refresh. Shared video quality still chooses "
        "tracks inside the ladder."
    ),
)
_REGION = Setting(
    "live_region",
    "BBC One region",
    options=[
        Option("london", "London"),
        Option("south", "South"),
        Option("south_east", "South East"),
        Option("east", "East"),
        Option("east_midlands", "East Midlands"),
        Option("west_midlands", "West Midlands"),
        Option("west", "West"),
        Option("south_west", "South West"),
        Option("channel_islands", "Channel Islands"),
        Option("yorkshire", "Yorkshire"),
        Option("east_yorkshire", "East Yorkshire & Lincolnshire"),
        Option("north_east", "North East & Cumbria"),
        Option("north_west", "North West"),
        Option("scotland", "Scotland"),
        Option("wales", "Wales"),
        Option("northern_ireland", "Northern Ireland"),
    ],
    default="london",
)

class BBCiPlayer(Service):
    ID = "bbc"
    NAME = "BBC iPlayer"
    TAG = "iP"
    LEGACY_IDS = ("bbcsounds",)
    ALIASES = (
        "iplayer",
        "bbciplayer",
        "bbc-tv",
        "sounds",
        "bbc-sounds",
        "bbc sounds",
        "bbcsounds",
        "bbcradio",
        "snds",
    )
    TITLE_RE = r"(?:www\.)?bbc\.(?:co\.uk|com)/(?:iplayer/(?:episode|episodes|live)/|programmes/|sounds/)"
    GEOFENCE = ("GB",)
    MEDIA_TYPES = ("audio", "video")
    DESCRIPTION = "BBC television, radio and podcasts. On-demand and live, clear up to UHD."
    USES = Capabilities()
    SETTINGS = [_RESOLUTION, _REGION]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    TOKEN_FILE = api.TOKEN_FILE

    def auth_status(self) -> AuthStatus:
        state = self._account_state()
        if state.signed_in:
            return AuthStatus(
                True, state.display_name or "BBC account",
                detail="TV session" if state.is_fresh() else "TV session · refresh on use",
                anonymous_ok=True, logout_available=True,
            )
        return AuthStatus(False, "anonymous · no sign-in needed", anonymous_ok=True)

    def _account_state(self) -> api.Session:
        tokens = getattr(self.ctx, "tokens", None)
        return api.Session.from_cache(tokens.read(self.TOKEN_FILE) if tokens is not None else None)

    def _save_account(self, state: api.Session) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def account_client(self) -> api.AccountClient:
        client = getattr(self, "_account", None)
        if client is None:
            client = api.AccountClient(
                self.ctx.session(user_agent=api.USER_AGENT_TV, cookies=False),
                state=self._account_state(), on_save=self._save_account,
            )
            self._account = client
        return client

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        page = ctx.pick(
            "BBC iPlayer sign-in",
            [Choice("TV code", "tv"), Choice("Continue anonymously", "anonymous")],
        )
        while True:
            try:
                method = yield page
            except Back:
                return
            if method == "anonymous":
                self.logout()
                ctx.log("BBC iPlayer anonymous mode enabled", "ok")
                return
            if method != "tv":
                return
            page.cursor = 0
            try:
                client = self.account_client()
                ctx.status("Requesting a BBC TV code")
                challenge = client.start()

                waiting = ctx.wait_for(
                    "Sign in to BBC iPlayer",
                    [("Open", challenge.verification_url), ("Code", challenge.user_code)],
                    poll=None, timeout=challenge.expires_in, interval=challenge.interval,
                    qr=QrPresentation(payload=challenge.verification_url, alt="BBC TV sign-in"),
                )

                def poll(client=client, challenge=challenge, waiting=waiting):
                    try:
                        confirmed = client.poll(challenge)
                        waiting.interval = challenge.interval
                        return confirmed
                    except _FLOW_ERRORS as exc:
                        return api.AuthError(str(exc))

                waiting.poll = poll
                confirmed = yield waiting
                if isinstance(confirmed, api.AuthError):
                    raise confirmed
                if not isinstance(confirmed, api.Session) or not confirmed.signed_in:
                    raise api.AuthError("BBC TV sign-in completed without an account session")
                ctx.log("BBC iPlayer TV sign-in completed", "ok")
                return
            except Back:
                pass
            except _FLOW_ERRORS as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        client = getattr(self, "_account", None)
        if client is not None:
            client.session.close()
        self._account = None

    def _account_headers(self, url: str) -> dict[str, str]:
        if not self._account_state().signed_in and getattr(self, "_account", None) is None:
            return {}
        return self.account_client().headers_for(url)

    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        def choices():
            status = self.auth_status()
            rows = [
                Choice("VOD - open a URL or content ID", "url"),
                Choice("Live TV and radio", "live"),
                Choice("Search", "search"),
                Choice("Sign in", "login", detail=status.label),
                Choice("Settings", "settings"),
            ]
            if status.logged_in:
                rows.append(Choice("Sign out", "logout", detail=status.label))
            return rows

        page = ctx.pick("", choices(), scope=SCOPE_ROOT)

        def visit(action):
            try:
                if action == "url":
                    yield from _browse_input(ctx, "Enter URL or content ID", self.open_url)
                elif action == "search":
                    yield from _browse_input(ctx, "Search", self.search)
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "login":
                    yield from self.begin_login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("BBC iPlayer signed out", "ok")
                elif action == "settings":
                    yield ctx.settings_request()
            finally:
                page.choices = choices()

        yield from _browse_page(ctx, page, visit)

    def client(self, ctx: FlowContext | None = None) -> api.IPlayerApi:
        return api.IPlayerApi(
            session=self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            resolution=self.settings.get("source_resolution") or "auto",
            region=str(self.settings.get("live_region") or "london"),
            authorization=self._account_headers,
        )

    def sounds_client(self) -> sounds.SoundsApi:
        return sounds.SoundsApi(
            session=self.ctx.session(
                user_agent=sounds.USER_AGENT,
                headers={"Accept": "application/json"},
                cookies=False,
            )
        )

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        if sounds.is_sounds_target(target):
            parsed = sounds.parse_input(target)
            if parsed is None:
                ctx.error(f"That does not look like a BBC Sounds link or PID: {target}")
                return
            yield from self._open_sounds(ctx, parsed)
            return
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like a BBC iPlayer or BBC Sounds link: {target}\n"
                "Expected /iplayer/episode|episodes|live/<pid>, /sounds/play|brand|series/<pid>, "
                "/programmes/<pid>, or a BBC PID."
            )
            return
        try:
            client = self.client(ctx)
            if parsed.kind == "programme":
                yield from self._show(ctx, client, client.show(parsed.pid), parsed.series_id)
            elif parsed.kind == "episode":
                yield from self._emit_episode(ctx, client, client.episode(parsed.pid))
            elif parsed.kind == "live":
                if _looks_like_radio(parsed.pid):
                    yield from self._emit_sounds_live(ctx, self.sounds_client(), parsed.pid)
                else:
                    yield from self._emit_live(ctx, client, client.channel(parsed.pid))
            else:
                try:
                    item = client.episode(parsed.pid)
                except api.IPlayerError:
                    try:
                        yield from self._show(ctx, client, client.show(parsed.pid), parsed.series_id)
                    except api.IPlayerError:
                        yield from self._open_sounds(ctx, sounds.ParsedInput("episode", parsed.pid))
                else:
                    yield from self._emit_episode(ctx, client, item)
        except api.IPlayerError as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        tv_hits: list[api.SearchHit] = []
        sounds_hits: list[sounds.SoundsHit] = []
        tv_error = ""
        sounds_error = ""
        try:
            client = self.client(ctx)
            ctx.status(f"Searching BBC iPlayer for {query}")
            tv_hits = client.search(query)
        except api.IPlayerError as exc:
            tv_error = str(exc)
        try:
            ctx.status(f"Searching BBC Sounds for {query}")
            sounds_hits = self.sounds_client().search(query)
        except sounds.SoundsError as exc:
            sounds_error = str(exc)
        if not tv_hits and not sounds_hits:
            if tv_error and not sounds_error:
                ctx.error(tv_error)
            elif sounds_error and not tv_error:
                ctx.error(sounds_error)
            elif tv_error and sounds_error:
                ctx.error(f"{tv_error}; {sounds_error}")
            else:
                ctx.warn(f"BBC iPlayer found nothing for {query}")
            return
        choices = [
            Choice(
                hit.title,
                hit.url,
                detail=hit.synopsis[:90],
                tags=tuple(value for value in ("TV", hit.kind, hit.category) if value),
            )
            for hit in tv_hits
        ]
        choices.extend(
            Choice(
                hit.title,
                hit.url,
                detail=(hit.synopsis or hit.network)[:90],
                tags=tuple(value for value in ("Sounds", hit.category or hit.kind, hit.network) if value),
            )
            for hit in sounds_hits
        )
        yield from _browse_page(
            ctx,
            ctx.pick(f"BBC iPlayer  ·  {query}", choices),
            lambda chosen: self.open_url(ctx, str(chosen)),
        )

    def _show(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        show: api.Show,
        selected: str = "",
    ) -> Iterator[Ask]:
        if not show.seasons:
            ctx.warn(f"BBC returned no seasons for {show.title}")
            return
        pages: dict[str, Pick] = {}
        if len(show.seasons) == 1:
            yield from self._season_episodes(ctx, client, show, show.seasons[0], pages)
            return
        page = ctx.pick(
            f"{show.title}  ·  seasons",
            [Choice(entry.label, entry) for entry in show.seasons],
            cursor=_season_cursor(show.seasons, selected),
        )
        yield from _browse_page(ctx, page, lambda season: self._season_episodes(ctx, client, show, season, pages))

    def _season_episodes(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        show: api.Show,
        season: api.Season,
        pages: dict[str, Pick] | None = None,
    ) -> Iterator[Ask]:
        pages = {} if pages is None else pages
        if season.id not in pages:
            try:
                ctx.status(f"Loading {show.title}  ·  {season.title}")
                loaded = client.season(season)
            except api.IPlayerError as exc:
                ctx.error(str(exc))
                return
            if not loaded.episodes:
                ctx.warn(f"BBC returned no episodes for {season.title}")
                return
            pages[season.id] = ctx.pick(
                f"{show.title}  ·  {loaded.label}",
                [Choice(item.label, item, detail=item.synopsis[:90]) for item in loaded.episodes],
                multi=True,
                hint="space to tick, enter to confirm",
            )

        def visit(picked):
            episodes = [item for item in (picked or []) if isinstance(item, api.Episode)]
            if len(episodes) > 1:
                ctx.batch(len(episodes))
            for item in episodes:
                try:
                    yield from self._emit_episode(ctx, client, item)
                except _FLOW_ERRORS as exc:
                    ctx.error(str(exc))

        yield from _browse_page(ctx, pages[season.id], visit)


    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        page = ctx.pick(
            "BBC iPlayer  ·  live",
            [
                Choice("TV", "tv", detail="Regional television"),
                Choice("Radio", "radio", detail="BBC Sounds stations"),
            ],
        )
        yield from _browse_page(ctx, page, lambda kind: self._live_radio(ctx) if kind == "radio" else self._live_tv(ctx))

    def _live_tv(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading BBC live channels")
            channels = client.channels()
        except api.IPlayerError as exc:
            ctx.error(str(exc))
            return
        page = ctx.table(
            f"BBC iPlayer  ·  {len(channels)} live channels",
            ["Channel", "Now", "Next"],
            [(channel.name, channel.now, channel.next) for channel in channels],
            channels,
        )
        yield from _browse_page(ctx, page, lambda chosen: self._live_tv_channel(ctx, client, chosen))

    def _live_tv_channel(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        channel: api.Channel,
    ) -> Iterator[Ask]:
        variants = client.regional_variants(channel)
        if len(variants) <= 1:
            chosen = client.channel(channel.id)
            yield from self._emit_live(ctx, client, chosen)
            return
        page = ctx.pick(
            f"{channel.name}  ·  regions",
            [Choice(variant.region or variant.name, variant, detail=variant.id) for variant in variants],
        )
        yield from _browse_page(ctx, page, lambda chosen: self._emit_live(ctx, client, client.channel(chosen.id)))

    def _live_radio(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.sounds_client()
        ctx.status("Loading stations")
        stations = client.stations()
        if not stations:
            ctx.warn("BBC Sounds returned no live stations")
            return
        page = ctx.table(
            "Live radio",
            ["Station", "Now"],
            [(station.programme, station.episode) for station in stations],
            stations,
        )
        yield from _browse_page(ctx, page, lambda target: self._emit_sounds_live(ctx, client, target.id, station=target))

    def _emit_live(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        channel: api.Channel,
    ) -> Iterator[Ask]:
        title = Title(
            id=channel.id,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            episode_name=channel.now or None,
            synopsis=channel.synopsis or None,
            cover_url=_cover_url(channel.poster_url, channel.image_url),
            service=self.ID,
        )
        yield from self._emit(ctx, client, title, channel, is_live=True)


    def _emit_episode(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        item: api.Episode,
    ) -> Iterator[Ask]:
        episodic = not item.film
        title = Title(
            id=item.id,
            kind=TitleKind.EPISODE if episodic else TitleKind.MOVIE,
            name=item.title,
            year=(item.year or None) if item.film else None,
            season=item.season if episodic else None,
            episode=item.number if episodic else None,
            episode_name=item.name if episodic else None,
            genre=item.category or None,
            synopsis=item.synopsis or None,
            cover_url=_cover_url(item.poster_url, item.image_url),
            service=self.ID,
        )
        yield from self._emit(ctx, client, title, item, is_live=item.live)

    def _emit(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        title: Title,
        item: api.Episode | api.Channel,
        *,
        is_live: bool,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {title.label()}")
            sources = client.sources(item)
        except api.IPlayerError as exc:
            ctx.error(f"{title.label()}: {exc}")
            return
        source = sources[0]
        save_name = self.save_name(title)
        if not title.cover_url:
            title.cover_url = _cover_url(source.poster_url, source.image_url)
        chapters = [
            Chapter(
                start_ms=max(0, int(value.get("start_ms") or 0)),
                title=str(value.get("title") or value.get("kind") or "Chapter"),
                kind=str(value.get("kind") or ""),
            )
            for value in source.chapters
            if isinstance(value, dict)
        ]
        drm = DrmInfo(clear=True)
        drm.context["bbc_manifest_sources"] = tuple(sources[1:])
        playback = Playback(
            title=title,
            save_name=save_name,
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=is_live,
            drm=drm,
            subtitle_references=self._subtitle_references(sources, is_live=is_live),
            chapters=chapters,
            attachments=self._tv_attachments(item, source),
            merge_manifests=not is_live and len(sources) > 1,
            note=" + ".join(value.line() for value in sources),
        )
        yield ctx.emit(playback)

    def manifest_variants(self, playback: Playback, log) -> list[Playback]:
        if playback.is_live or not playback.merge_manifests or playback.drm is None:
            return []
        sources = playback.drm.context.get("bbc_manifest_sources")
        if not isinstance(sources, tuple):
            return []
        variants: list[Playback] = []
        for source in sources:
            if not isinstance(source, api.Source):
                continue
            variants.append(
                Playback(
                    title=playback.title,
                    save_name=playback.save_name,
                    manifest_url=source.manifest,
                    headers=dict(playback.headers),
                    proxy=playback.proxy,
                    drm=DrmInfo(clear=True),
                    attachments=list(playback.attachments),
                    merge_manifests=False,
                    note=source.line(),
                )
            )
            log(f"BBC iPlayer merged source: {source.line()}")
        return variants

    def _subtitle_references(
        self,
        sources: list[api.Source],
        *,
        is_live: bool,
    ) -> list[SubtitleReference]:
        if is_live:
            return []
        references: list[SubtitleReference] = []
        seen: set[str] = set()
        for source in sources:
            url = str(source.subtitle or "").strip()
            if not url:
                continue
            key = url.split("?", 1)[0].casefold()
            if key in seen:
                continue
            seen.add(key)
            references.append(
                SubtitleReference(
                    url=url,
                    language="en",
                    kind="sdh",
                    name="English",
                )
            )
        return references

    def augment_tracks(self, playback: Playback, tracks, log):
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
            rows.append(
                StreamInfo(
                    manifest_type="service",
                    media_type="subtitle",
                    url=url,
                    original_url=url,
                    id=f"bbc-subtitle-{index}",
                    group_id=reference.kind or "sdh",
                    name=reference.name or "English",
                    language=reference.language or "en",
                    role="SDH" if (reference.kind or "").casefold() == "sdh" else None,
                    codecs="subrip",
                    extension="srt",
                    extra={
                        "service_sidecar": "subtitle",
                        "subtitle_reference_url": url,
                        "subtitle_kind": reference.kind or "sdh",
                    },
                )
            )
        if rows:
            log(f"BBC iPlayer: added {len(rows)} API subtitle track(s) to the picker")
        return rows

    def prepare_download(self, playback: Playback, log) -> None:
        selected = [subtitle for subtitle in playback.subtitle_references if subtitle.selected]
        if not selected:
            return
        client = self.client()
        directory = self.ctx.subtitle_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        imports: list[ExternalTrack] = []
        for index, subtitle in enumerate(selected, 1):
            data = client.subtitle(subtitle.url)
            clean_url = subtitle.url.lower().split("?", 1)[0]
            suffix = next(
                (value for value in (".srt", ".vtt", ".ttml", ".dfxp", ".xml") if clean_url.endswith(value)),
                ".ttml" if b"<tt" in data[:1000].lower() else ".xml",
            )
            digest = hashlib.sha256(subtitle.url.encode("utf-8") + b"\0" + data).hexdigest()[:12]
            stem = re.sub(
                r"[^\w.-]+",
                ".",
                f"{playback.save_name}.{subtitle.language}.{subtitle.kind}.{index}.{digest}",
            )
            stem = re.sub(r"\.{2,}", ".", stem).strip(".")[:180] or "BBC.iPlayer"
            original = directory / f"{stem}{suffix}"
            if not original.exists() or original.read_bytes() != data:
                original.write_bytes(data)
                os.chmod(original, 0o600)
            selected_path = original
            imports.append(ExternalTrack(str(selected_path), subtitle.language or "en", subtitle.name or "English"))
        playback.mux_imports.extend(imports)
        log(f"BBC iPlayer subtitles: prepared {len(imports)}/{len(selected)} track(s)")

    def _open_sounds(self, ctx: FlowContext, parsed: sounds.ParsedInput) -> Iterator[Ask]:
        client = self.sounds_client()
        if parsed.kind == "live":
            yield from self._emit_sounds_live(ctx, client, parsed.value)
            return
        if parsed.kind == "container":
            yield from self._browse_sounds_container(ctx, client, parsed.value)
            return
        ctx.status(f"Looking up {parsed.value}")
        try:
            item = client.episode(parsed.value)
        except sounds.SoundsError as exc:
            ctx.warn(f"{exc}; trying it as a series")
            yield from self._browse_sounds_container(ctx, client, parsed.value)
            return
        yield from self._emit_sounds_item(ctx, client, item)

    def _browse_sounds_container(
        self,
        ctx: FlowContext,
        client: sounds.SoundsApi,
        container_id: str,
    ) -> Iterator[Ask]:
        history = [0]
        pages: dict[int, Pick] = {}
        while True:
            offset = history[-1]
            if offset not in pages:
                try:
                    ctx.status(f"Loading episodes {offset + 1}+")
                    items, total = client.container(container_id, offset=offset)
                except _FLOW_ERRORS as exc:
                    ctx.error(str(exc))
                    if len(history) == 1:
                        return
                    history.pop()
                    continue
                if not items:
                    ctx.warn("No episodes available")
                    if len(history) == 1:
                        return
                    history.pop()
                    continue
                choices = [Choice(item.episode, item, detail=_sounds_detail(item)) for item in items]
                if offset + len(items) < total:
                    choices.append(Choice(f"Next {sounds.PAGE_SIZE} of {total}...", "__more__", navigates=True))
                pages[offset] = ctx.pick(
                    f"{items[0].programme}  ·  {total} episodes  ·  page {offset // sounds.PAGE_SIZE + 1}",
                    choices,
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            page = pages[offset]
            try:
                picked = yield page
            except Back:
                if len(history) == 1:
                    return
                history.pop()
                continue
            if not picked:
                return
            wanted = [item for item in (picked or []) if item != "__more__" and isinstance(item, sounds.SoundsItem)]
            paging = any(item == "__more__" for item in (picked or []))
            page.preselected = [index for index, choice in enumerate(page.choices) if choice.value in wanted]
            page.cursor = next(
                (index for index, choice in enumerate(page.choices) if choice.value in picked), page.cursor
            )
            try:
                if len(wanted) > 1:
                    ctx.batch(len(wanted))
                for item in wanted:
                    try:
                        yield from self._emit_sounds_item(ctx, client, item)
                    except _FLOW_ERRORS as exc:
                        ctx.error(str(exc))
            except Back:
                if not ctx.interactive:
                    return
                continue
            if paging:
                history.append(offset + sounds.PAGE_SIZE)
                continue
            if not ctx.interactive:
                return

    def _emit_sounds_item(
        self,
        ctx: FlowContext,
        client: sounds.SoundsApi,
        item: sounds.SoundsItem,
    ) -> Iterator[Ask]:
        ctx.status(f"Resolving {item.episode}")
        try:
            source = client.source_for(item)
        except sounds.SoundsError as exc:
            ctx.error(f"{item.episode}: {exc}")
            return
        yield ctx.emit(self._sounds_playback(item, source))

    def _emit_sounds_live(
        self,
        ctx: FlowContext,
        client: sounds.SoundsApi,
        service_id: str,
        station: sounds.SoundsItem | None = None,
    ) -> Iterator[Ask]:
        ctx.status(f"Resolving {service_id}")
        try:
            source = client.live_source_for(service_id)
        except sounds.SoundsError as exc:
            ctx.error(f"{service_id}: {exc}")
            return
        if station is None:
            station = next((entry for entry in client.stations() if entry.id == service_id), None)
        item = station or sounds.SoundsItem(id=service_id, programme=service_id, episode="Live", live=True)
        item.live = True
        yield ctx.emit(self._sounds_playback(item, source))

    def _sounds_playback(self, item: sounds.SoundsItem, source: sounds.AudioSource) -> Playback:
        title = Title(
            id=item.id,
            kind=TitleKind.STATION if item.live else TitleKind.TRACK,
            name=item.programme,
            episode_name=item.episode if item.episode != item.programme else None,
            year=(item.release_date or "")[:4] or None,
            duration=item.duration,
            channel=item.network or None,
            artist=item.programme,
            album=item.album,
            genre="Podcast" if not item.live else "Radio",
            publisher="BBC",
            synopsis=item.synopsis,
            cover_url=api.expand_image(item.image_url, "1024x1024") or None,
            service=self.ID,
        )
        note = f"{source.quality}"
        if source.direct_file:
            note += " · progressive download, already MP3"
        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.url,
            headers={"User-Agent": sounds.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=item.live,
            merge_manifests=False,
            attachments=self._image_attachments(
                (item.image_url, "Artwork", "artwork"),
                user_agent=sounds.USER_AGENT,
            ),
            note=note,
        )

    def _tv_attachments(self, item: api.Episode | api.Channel, source: api.Source) -> list[Attachment]:
        poster = source.poster_url or item.poster_url
        return self._image_attachments(
            (poster, "Poster", "poster"),
            (source.image_url or item.image_url, "Artwork" if poster else "Poster", "image" if poster else "poster"),
            (item.poster_url, "Catalogue poster", "poster"),
            (item.image_url, "Catalogue artwork", "image"),
            (source.banner_url, "Banner", "image"),
            (item.banner_url, "Catalogue banner", "image"),
            user_agent=api.USER_AGENT,
        )

    def _image_attachments(
        self,
        *rows: tuple[str, str, str],
        user_agent: str,
    ) -> list[Attachment]:
        if not self.fetch_attachments_enabled():
            return []
        attachments: list[Attachment] = []
        seen: set[str] = set()
        headers = {"User-Agent": user_agent}
        for url, name, kind in rows:
            expanded = _image_url(url)
            if not expanded or expanded in seen:
                continue
            seen.add(expanded)
            suffix = urlsplit(expanded).path.rsplit(".", 1)[-1].lower()
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(suffix, "")
            attachments.append(Attachment(expanded, name=name, kind=kind, mime_type=mime, headers=headers))
        return attachments


def _season_cursor(seasons: list[api.Season], selected: str) -> int:
    wanted = str(selected or "").strip().casefold()
    if not wanted:
        return 0
    for index, entry in enumerate(seasons):
        slice_id = str(entry.id or "").casefold()
        if not slice_id:
            continue
        if slice_id == wanted or slice_id.endswith(wanted) or wanted.endswith(slice_id):
            return index
    return 0


def _cover_url(*urls: str) -> str | None:
    for url in urls:
        expanded = _image_url(url)
        if expanded:
            return expanded
    return None


def _image_url(url: str) -> str:
    if not isinstance(url, str):
        return ""
    expanded = api.expand_image(url)
    try:
        parsed = urlsplit(expanded)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char.isspace() for char in expanded)
            or "{" in expanded
            or "}" in expanded
        ):
            return ""
    except ValueError:
        return ""
    return expanded


def _looks_like_radio(pid: str) -> bool:
    return "radio" in str(pid or "").lower()


def _sounds_detail(item: sounds.SoundsItem) -> str:
    bits = [item.release_date, _duration(item.duration), item.synopsis]
    return "  ·  ".join(bit for bit in bits if bit)


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, _ = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


registry.register(BBCiPlayer)

__all__ = ["BBCiPlayer", "api", "sounds"]

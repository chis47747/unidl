"""ZDF Mediathek - anonymous or TV-code sign-in, clear HLS/DASH VOD and live."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import replace
from functools import partial

from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext, Pick
from ...core.playback import ExternalTrack, Playback, SubtitleReference
from ...core.secureio import atomic_write_bytes, private_directory
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from ...downloader.subtitles import SubtitleConversionError, convert_subtitle_file
from . import api


@registry.register
class Zdf(Service):
    ID = "zdf"
    NAME = "ZDF Mediathek"
    TAG = "ZDF"
    ALIASES = ("zdfmediathek", "zdf-mediathek")
    TITLE_RE = r"^(?:(?:https?:)?//)?(?:[A-Za-z0-9-]+\.)*zdf\.de(?::\d+)?/"
    GEOFENCE = ("DE",)
    DESCRIPTION = (
        "ZDF Mediathek VOD and live channels. Anonymous or TV-code sign-in; clear HLS/DASH."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ()
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = "zdf_token.json"
    TV_TOKEN_FILE = "zdf_tv.json"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.ZdfApi | None = None

    def _load_tv_state(self) -> dict | None:
        state = self.ctx.tokens.read(self.TV_TOKEN_FILE)
        if state is None and self.ctx.tokens.resolve(self.TV_TOKEN_FILE) is not None:
            return {}
        return state

    def auth_status(self) -> AuthStatus:
        tv_cache = self._load_tv_state()
        tv = api.tv_auth_from_cache(tv_cache)
        if tv is not None:
            return AuthStatus(
                logged_in=True,
                anonymous_ok=True,
                label="TV code signed in",
                detail="account token cache; refreshes when needed",
            )
        if tv_cache is not None:
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                logout_available=True,
                label="TV session invalid; sign in again or choose Anonymous",
            )
        cached = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        if api.cache_is_valid(cached):
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                label="anonymous token cached",
                detail="token cache",
            )
        if cached:
            state = "expired" if cached.get("token") else "invalid"
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                label=f"anonymous token cache {state}",
                detail="will refresh on the next request",
            )
        return AuthStatus(
            logged_in=False,
            anonymous_ok=True,
            label="no sign-in needed",
        )

    def _save_state(self, state: dict) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state)

    def _clear_state(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)

    def _save_tv_state(self, state: dict) -> None:
        self.ctx.tokens.write(self.TV_TOKEN_FILE, state)

    def _clear_tv_state(self) -> None:
        self.ctx.tokens.remove(self.TV_TOKEN_FILE)

    def _expire_tv_state(self) -> None:
        self.ctx.tokens.write(self.TV_TOKEN_FILE, {"expired": True})

    def _new_client(self) -> api.ZdfApi:
        def session_factory():
            return self.ctx.session(user_agent=api.USER_AGENT, cookies=False)

        return api.ZdfApi(
            session=session_factory(),
            session_factory=session_factory,
            on_save=self._save_state,
            on_clear=self._clear_state,
            on_tv_save=self._save_tv_state,
            on_tv_clear=self._expire_tv_state,
        )

    def client(self, ctx: FlowContext | None = None) -> api.ZdfApi:
        if self._api is not None:
            return self._api
        client = self._new_client()
        tv_cache = self._load_tv_state()
        tv = api.tv_auth_from_cache(tv_cache)
        if tv_cache is not None and tv is None:
            client.session.close()
            raise api.ZdfError("Stored ZDF TV session is invalid; sign in again or choose Anonymous")
        if tv is not None:
            client.set_tv_auth(tv)
        cached = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        if not client.adopt(cached):
            if cached:
                self._clear_state()
            if ctx:
                ctx.status("Getting a ZDF anonymous token")
            client.authenticate()
        self._api = client
        return client

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        page = ctx.pick(
            "Sign in",
            [Choice("Anonymous", "anonymous"), Choice("TV code", "tv")],
        )
        while True:
            method = yield page
            page.cursor = 1 if method == "tv" else 0
            if method not in {"anonymous", "tv"}:
                return
            candidate = self._new_client()
            try:
                if method == "anonymous":
                    ctx.status("Getting a ZDF anonymous token")
                    candidate.authenticate()
                    self._clear_tv_state()
                else:
                    ctx.status("Creating a ZDF TV login code")
                    device = candidate.start_device_code()

                    def poll(client=candidate, challenge=device):
                        try:
                            return client.poll_device_code(challenge)
                        except api.ZdfError as exc:
                            return api.DevicePollFailure(str(exc))

                    state = yield ctx.wait_for(
                        "Confirm ZDF TV sign-in",
                        [("Open", device.verification_uri), ("Code", device.user_code)],
                        poll,
                        timeout=float(device.expires_in),
                        interval=float(device.interval),
                    )
                    if isinstance(state, api.DevicePollFailure):
                        ctx.error(state.message)
                        if not ctx.interactive:
                            return
                        continue
                    if not isinstance(state, api.TvAuth):
                        ctx.error("ZDF TV sign-in finished without a usable session")
                        if not ctx.interactive:
                            return
                        continue
                    candidate.set_tv_auth(state, save=True)
                previous = self._api
                self._api = candidate
                candidate = None
                if previous is not None:
                    previous.session.close()
                ctx.log("ZDF TV code signed in" if method == "tv" else "ZDF anonymous session ready", "ok")
                return
            except Back:
                pass
            except (api.ZdfError, OSError) as exc:
                ctx.error(str(exc))
            finally:
                if candidate is not None:
                    candidate.session.close()
                ctx.status("")
            if not ctx.interactive:
                return

    def logout(self) -> None:
        if self._api is not None:
            self._api.clear_token()
            self._api.session.close()
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._clear_tv_state()
        self._api = None
        super().logout()

    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        page = ctx.pick(
            "",
            [
                Choice("VOD - open a URL or content ID", "url"),
                Choice("Live TV", "live"),
                Choice("Search", "search"),
                Choice("Sign in", "login"),
                Choice("Settings", "settings"),
            ],
            scope=SCOPE_ROOT,
        )
        while True:
            status = self.auth_status()
            page.choices = [choice for choice in page.choices if choice.value != "logout"]
            for choice in page.choices:
                if choice.value == "login":
                    choice.detail = status.label
            if status.logged_in or status.logout_available:
                page.choices.append(Choice("Sign out", "logout", detail=status.label))
            page.cursor = min(page.cursor, len(page.choices) - 1)
            action = yield page
            page.cursor = next(
                (index for index, choice in enumerate(page.choices) if choice.value == action),
                page.cursor,
            )
            try:
                if action == "url":
                    yield from self._input(ctx, "Enter URL or content ID", self._open_target)
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "search":
                    yield from self._input(ctx, "Search", self._search_results)
                elif action == "login":
                    yield from self.begin_login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("Signed out of ZDF", "ok")
                elif action == "settings":
                    yield ctx.settings_request()
                else:
                    return
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _input(
        self,
        ctx: FlowContext,
        title: str,
        visit: Callable[[FlowContext, str], Iterator[Ask]],
        initial: str | None = None,
    ) -> Iterator[Ask]:
        page = ctx.text(title)
        value = initial
        while True:
            if value is None:
                value = yield page
            wanted = str(value or "").strip()
            if not wanted:
                return
            page.default = wanted
            try:
                yield from visit(ctx, wanted)
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return
            value = None

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        yield from self._input(ctx, "Enter URL or content ID", self._open_target, target)

    def _open_target(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like a ZDF link or canonical: {target}\n"
                "Expected https://www.zdf.de/.../<canonical>.html or a bare canonical."
            )
            return
        try:
            client = self.client(ctx)
            ctx.status(f"Loading {parsed.canonical}")
            found = client.resolve(parsed.canonical)
        except api.ZdfError as exc:
            ctx.error(str(exc))
            return
        if isinstance(found, api.Video):
            yield from self._emit(ctx, client, found)
            return
        yield from self._collection(ctx, client, found)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        yield from self._input(ctx, "Search", self._search_results, query)

    def _search_results(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status(f"Searching ZDF for {query}")
            first = client.search_page(query)
        except api.ZdfError as exc:
            ctx.error(str(exc))
            return
        if not first.hits:
            ctx.warn(f"ZDF found nothing for {query}")
            return

        def results_page(result: api.SearchPage, number: int) -> Pick:
            choices = [
                Choice(
                    hit.label,
                    hit,
                    detail=hit.detail[:90],
                    tags=(hit.kind,) if hit.kind else (),
                )
                for hit in result.hits
            ]
            if result.has_next_page and result.end_cursor:
                choices.append(
                    Choice(
                        "Next page",
                        ("next", result.end_cursor),
                        detail="Load more ZDF search results",
                        navigates=True,
                    )
                )
            return ctx.pick(f"ZDF  ·  {query}  ·  page {number}", choices)

        # Keep each visited result page, including its selection, on the stack.
        pages = [(first, results_page(first, 1))]
        while pages:
            _result, page = pages[-1]
            try:
                chosen = yield page
            except Back:
                pages.pop()
                continue
            if chosen is None:
                return
            page.cursor = next(
                (index for index, choice in enumerate(page.choices) if choice.value == chosen),
                page.cursor,
            )
            if isinstance(chosen, tuple) and len(chosen) == 2 and chosen[0] == "next":
                try:
                    following = client.search_page(query, after=str(chosen[1]))
                    if following.has_next_page and following.end_cursor in {
                        visited.end_cursor for visited, _page in pages
                    }:
                        raise api.ZdfError("ZDF search returned a repeated page cursor")
                    if following.hits:
                        pages.append((following, results_page(following, len(pages) + 1)))
                    else:
                        ctx.warn("ZDF returned no more search results")
                except (RuntimeError, OSError) as exc:
                    ctx.error(str(exc))
                continue
            if not isinstance(chosen, api.SearchHit):
                return
            if not chosen.canonical:
                ctx.warn(f"{chosen.label} has no canonical to open")
                continue
            try:
                ctx.status(f"Loading {chosen.canonical}")
                if chosen.kind == "video":
                    video = client.video(chosen.canonical)
                    if video is None:
                        ctx.error(f"ZDF could not load video {chosen.canonical}")
                        continue
                    yield from self._emit(ctx, client, video)
                elif chosen.kind == "collection":
                    found = client.resolve(chosen.canonical)
                    if isinstance(found, api.Video):
                        yield from self._emit(ctx, client, found)
                    else:
                        yield from self._collection(ctx, client, found)
                else:
                    ctx.warn(f"ZDF search result type {chosen.kind!r} is not playable")
            except Back:
                continue
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading ZDF live channels")
            channels = client.channels()
        except api.ZdfError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("ZDF returned no live channels")
            return
        page = ctx.table(
            f"ZDF Live  ·  {len(channels)} channels",
            ["Channel", "Type", "State"],
            [(channel.label, channel.content_type, channel.state or "") for channel in channels],
            channels,
        )
        while True:
            try:
                chosen = yield page
            except Back:
                return
            if chosen is None:
                return
            try:
                ctx.status(f"Loading {chosen.label}")
                video = client.video(chosen.canonical) if chosen.canonical else None
                if video is None:
                    ctx.error(f"ZDF could not load live channel {chosen.canonical}")
                    continue
                yield from self._emit(ctx, client, video, is_live=True)
            except Back:
                continue
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _collection(
        self,
        ctx: FlowContext,
        client: api.ZdfApi,
        collection: api.Collection,
    ) -> Iterator[Ask]:
        if collection.movie is not None:
            try:
                yield from self._emit(ctx, client, collection.movie)
            except Back:
                return
            return

        options: list[tuple[str, str, object]] = []
        for season in collection.seasons:
            options.append(("season", season.label, season))
        if collection.sections:
            options.append(
                ("videos", f"Sections  ·  {len(collection.sections)} item(s)", collection.sections)
            )
        if collection.extras:
            options.append(
                ("videos", f"Extras  ·  {len(collection.extras)} item(s)", collection.extras)
            )

        if not options:
            ctx.warn(f"ZDF returned no playable VOD for {collection.title}")
            return

        counts = []
        if collection.count_seasons is not None:
            counts.append(f"{collection.count_seasons} seasons")
        if collection.count_episodes is not None:
            loaded_episodes = sum(len(season.episodes) for season in collection.seasons)
            if loaded_episodes != collection.count_episodes:
                counts.append(
                    f"{loaded_episodes}/{collection.count_episodes} playable episodes"
                )
            else:
                counts.append(f"{collection.count_episodes} episodes")
        if counts:
            ctx.log(f"{collection.title} · {' · '.join(counts)}", "ok")

        catalogue = ctx.pick(
            f"{collection.title}  ·  catalogue",
            [Choice(label, (kind, payload)) for kind, label, payload in options],
        )
        while True:
            try:
                if len(options) == 1 and options[0][0] == "season":
                    selected_kind, _label, payload = options[0]
                else:
                    picked = yield catalogue
                    if picked is None:
                        return
                    catalogue.cursor = next(
                        (index for index, choice in enumerate(catalogue.choices) if choice.value == picked),
                        catalogue.cursor,
                    )
                    selected_kind, payload = picked
            except Back:
                return

            if selected_kind == "season":
                season = payload
                assert isinstance(season, api.Season)
                videos = season.episodes
                heading = f"{collection.title}  ·  {season.label}"
            else:
                videos = list(payload)  # type: ignore[arg-type]
                label = options[catalogue.cursor][1]
                heading = f"{collection.title}  ·  {label}"

            if not videos:
                ctx.warn(f"{heading}: nothing loaded")
                if len(options) == 1:
                    return
                continue

            page = ctx.pick(
                heading,
                [Choice(item.label, item, detail=(item.synopsis or item.year)[:90]) for item in videos],
                multi=True,
                hint="space to tick, enter to confirm",
            )
            while True:
                try:
                    selected = yield page
                except Back:
                    if len(options) == 1:
                        return
                    break
                items = list(selected or [])
                if not items:
                    if len(options) == 1:
                        return
                    break
                indexes = [index for index, choice in enumerate(page.choices) if choice.value in items]
                if indexes:
                    page.cursor = indexes[0]
                    page.preselected = indexes
                if len(items) > 1:
                    ctx.batch(len(items))
                for item in items:
                    try:
                        yield from self._emit(ctx, client, item)
                    except Back:
                        break
                    except (RuntimeError, OSError) as exc:
                        ctx.error(str(exc))
                        break
                if not ctx.interactive:
                    return

    def _emit(
        self,
        ctx: FlowContext,
        client: api.ZdfApi,
        video: api.Video,
        *,
        is_live: bool | None = None,
    ) -> Iterator[Ask]:
        live = video.is_live if is_live is None else is_live
        ctx.status(f"Resolving {video.label}")
        video = client.ensure_playable(video)
        source = client.source(video)
        ctx.log(f"{video.label}: {source.line()}")
        metadata = api.PlaybackMetadata()
        chapters = self.fetch_chapters_enabled() and not live
        attachments = self.fetch_attachments_enabled()
        if chapters or attachments:
            try:
                metadata = client.playback_metadata(video, chapters=chapters, attachments=attachments)
            except (RuntimeError, OSError, TypeError, ValueError) as exc:
                ctx.warn(f"ZDF optional playback metadata unavailable: {exc}")
        for warning in metadata.warnings:
            ctx.warn(warning)

        if live:
            kind = TitleKind.CHANNEL
            name = video.title
            season = None
            episode = None
            episode_name = None
        elif video.is_movie or (
            video.season is None and video.episode is None and not video.series_title
        ):
            kind = TitleKind.MOVIE
            name = video.title
            season = None
            episode = None
            episode_name = None
        else:
            kind = TitleKind.EPISODE
            name = video.series_title or video.title
            season = video.season
            episode = video.episode
            episode_name = video.title if video.series_title and video.title != video.series_title else None

        title = Title(
            id=video.id or video.canonical,
            kind=kind,
            name=name,
            year=video.year or None,
            season=season,
            episode=episode,
            episode_name=episode_name,
            duration=video.duration,
            channel=video.title if live else None,
            synopsis=video.synopsis or None,
            service=self.ID,
        )
        # Clear adaptive streams: no DrmInfo at all (service never uses DRM).
        yield ctx.emit(
            Playback(
                title=title,
                save_name=self.save_name(title),
                manifest_url=source.url,
                headers={"User-Agent": api.USER_AGENT},
                proxy=self.ctx.proxy,
                is_live=live,
                note=source.line(),
                chapters=list(metadata.chapters),
                attachments=list(metadata.attachments),
                subtitle_references=list(source.subtitles),
            )
        )

    def augment_tracks(self, playback: Playback, tracks, log) -> list[StreamInfo]:
        del log
        seen = {
            stream.url for stream in tracks.streams
            if stream.media_type == "subtitle" and not stream.extra.get("portable_subtitle_reference")
        }
        playback.subtitle_references = [
            replace(reference, selected=False) if reference.url in seen else reference
            for reference in playback.subtitle_references
        ]
        rows: list[StreamInfo] = []
        for reference in playback.subtitle_references:
            if reference.url in seen:
                continue
            seen.add(reference.url)
            rows.append(StreamInfo(
                manifest_type="service",
                media_type="subtitle",
                url=reference.url,
                original_url=reference.url,
                id=f"zdf-subtitle-{len(rows) + 1}",
                group_id=reference.kind,
                name=reference.name or reference.language,
                language=reference.language,
                codecs={"vtt": "webvtt", "srt": "subrip", "ttml": "ttml"}.get(reference.format, ""),
                extension=reference.format,
                extra={
                    "service_sidecar": "subtitle",
                    "subtitle_reference_url": reference.url,
                    "subtitle_kind": reference.kind,
                },
            ))
        return rows

    def prepare_download(self, playback: Playback, log: Callable[[str], None]) -> None:
        self._prepare_subtitles(playback, log)

    def prepare_download_with_progress(
        self,
        playback: Playback,
        log: Callable[[str], None],
        progress: Callable[[SubtitleReference, int, int | None], None],
    ) -> None:
        self._prepare_subtitles(playback, log, progress)

    def _prepare_subtitles(
        self,
        playback: Playback,
        log: Callable[[str], None],
        progress: Callable[[SubtitleReference, int, int | None], None] | None = None,
    ) -> None:
        selected = [reference for reference in playback.subtitle_references if reference.selected]
        if not selected:
            return
        client = self.client()
        directory = private_directory(self.ctx.subtitle_dir())
        output_format = str(self.settings.get("sub_format", "srt") or "srt")
        for reference in selected:
            report = partial(progress, reference) if progress is not None else None
            data = client.subtitle_bytes(reference.url, progress=report)
            digest = hashlib.sha256(reference.url.encode() + b"\0" + data).hexdigest()[:20]
            original = directory / f"zdf.{digest}.{reference.format or 'vtt'}"
            try:
                atomic_write_bytes(original, data)
                converted = convert_subtitle_file(
                    original,
                    output_format=output_format,
                    output_path=original.with_suffix(f".{output_format}"),
                    auto_fix=bool(self.settings.get("auto_subtitle_fix", True)),
                    duration=playback.title.duration,
                )
                if not converted.is_file() or not converted.stat().st_size:
                    raise SubtitleConversionError("no subtitle output was produced")
                converted.chmod(0o600)
            except (OSError, SubtitleConversionError) as exc:
                raise api.ZdfError(f"Could not prepare ZDF subtitles: {exc}") from exc
            if not any(track.path == str(converted) for track in playback.mux_imports):
                playback.mux_imports.append(ExternalTrack(
                    path=str(converted),
                    language=reference.language,
                    name=reference.name,
                    kind=reference.kind,
                    original=reference.original,
                    forced=reference.kind == "forced",
                ))
            log(f"ZDF subtitle prepared: {reference.name or reference.language}")


__all__ = ["Zdf", "api"]

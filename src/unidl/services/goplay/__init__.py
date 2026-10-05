"""Play Belgium (GoPlay) TV-code login, catalogue, search, live and KeyOS Widevine.

Authorization: Play Android TV code with refresh-token reuse.
Geofence:      Belgium; catalogue visibility and playback depend on location.
Catalogue:     Android TV pages, programs, search and live streams.
Playback:      DASH preferred; protected video uses KeyOS Widevine.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import requests

from ...core.cdm import CdmError
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.qr import QrPresentation
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


@registry.register
class GoPlay(Service):
    ID = "goplay"
    NAME = "Play (Belgium)"
    MEDIA_TYPES = ("video",)
    TAG = "GOPLAY"
    ALIASES = ("goplay", "play.tv", "play belgium", "play tv", "goplay.be")
    TITLE_RE = r"(?:(?:www|mail|email)\.)?(?:play\.tv|goplay\.be)|clicks\.playmedia\.be"
    GEOFENCE = ("BE",)
    DESCRIPTION = "Play Belgium TV: TV-code sign-in, homepage, URLs, search, live, library and KeyOS Widevine playback."
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    SUPPORTS_LIBRARY = True

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._api: api.GoPlayApi | None = None

    # ------------------------------------------------------------------- auth
    def _save_state(self, state: api.Session) -> None:
        path = self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())
        path.chmod(0o600)

    def _new_client(self, state: api.Session | None = None) -> api.GoPlayApi:
        return api.GoPlayApi(
            self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            state or api.Session(),
            on_save=self._save_state,
        )

    def auth_status(self) -> AuthStatus:
        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if not state.signed_in:
            return AuthStatus(False, "TV device is not signed in")
        label = state.account_label or "Play account"
        if state.is_fresh():
            return AuthStatus(True, label, detail="TV token cache")
        if state.recoverable:
            return AuthStatus(True, f"{label} · refresh required", detail="TV token cache")
        return AuthStatus(False, "TV session expired", detail="Sign in with a new TV code")

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        try:
            ctx.status("Requesting a Play TV code")
            challenge = client.start_device_code()
        except api.GoPlayError as exc:
            ctx.error(str(exc))
            return

        login_url = challenge.uri or api.LOGIN_PAGE_URL
        try:
            state = yield ctx.wait_for(
                "Sign in to Play on another device",
                [
                    ("Code", challenge.code),
                    ("Open", login_url),
                ],
                poll=lambda: client.poll_device_code(challenge),
                timeout=float(challenge.expires_in),
                interval=float(challenge.interval),
                hint="Open the page, enter the TV code, then return here.",
                qr=QrPresentation(
                    payload=challenge.uri,
                    fallback_url=login_url,
                    alt="Play TV sign-in QR code",
                ),
            )
        except Back:
            return
        except api.GoPlayError as exc:
            ctx.error(str(exc))
            return
        if not isinstance(state, api.Session) or not state.is_fresh():
            ctx.error("Play TV login finished without a usable session")
            return
        self._api = client
        ctx.log(f"signed in · {state.account_label or 'Play account'}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None
        super().logout()

    def client(self, ctx: FlowContext | None = None) -> api.GoPlayApi:
        if self._api is not None:
            self._api.ensure_auth()
            self._save_state(self._api.state)
            return self._api

        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if not state.signed_in:
            raise api.AuthenticationRequired("Play is not signed in. Open Sign in and enter the TV code first.")
        client = self._new_client(state)
        if ctx:
            ctx.status("Opening the Play TV session")
        client.ensure_auth()
        self._save_state(client.state)
        self._api = client
        if ctx:
            ctx.log("Play TV session ready", "ok")
        return client

    # -------------------------------------------------------------------- home
    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        """Add the TV homepage browse entry to the framework's normal actions."""
        while True:
            status = self._auth_status_quietly()
            choices: list[Choice] = []
            if not status.usable:
                choices.append(Choice("Sign in", "login", detail=status.label))
            choices.extend(
                [
                    Choice("Browse Play", "browse", detail="Android TV homepage and catalogues"),
                    Choice("VOD - open a Play URL", "url"),
                    Choice("Live TV", "live"),
                    Choice("Search", "search"),
                    Choice("My library", "library"),
                    Choice("Settings", "settings"),
                ]
            )
            if status.usable:
                choices.append(Choice("Sign in again", "login", detail=status.label))
                choices.append(Choice("Sign out", "logout", detail=status.label))

            action = yield ctx.pick("", choices, scope=SCOPE_ROOT)
            try:
                if action == "browse":
                    yield from self._browse(ctx)
                elif action == "url":
                    target = yield ctx.text("Enter a Play URL")
                    yield from self.open_url(ctx, str(target or "").strip())
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "search":
                    query = yield ctx.text("Search Play")
                    yield from self.search(ctx, str(query or "").strip())
                elif action == "library":
                    yield from self.library(ctx)
                elif action == "login":
                    yield from self.begin_login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("signed out", "ok")
                elif action == "settings":
                    yield ctx.settings_request()
            except Back:
                continue
            except api.AuthenticationRequired as exc:
                ctx.error(str(exc))
            except api.AvailabilityError as exc:
                ctx.error(str(exc))
            except RuntimeError as exc:
                ctx.problem(
                    f"{self.NAME} could not finish that",
                    f"{type(exc).__name__}: {exc}",
                    "The service menu is still open - the rest of the session is fine.",
                )

    # --------------------------------------------------------------- catalogue
    def _browse(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        while True:
            try:
                slug = yield ctx.pick(
                    "Play catalogues",
                    [Choice(title, page_slug) for page_slug, title in api.BROWSE_PAGES],
                )
            except Back:
                return
            if not isinstance(slug, str) or not slug:
                return
            ctx.status(f"Loading the Play {slug} page")
            page = client.page(slug)
            yield from self._page_flow(ctx, client, page, title=page.title or slug)

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        text = str(target or "").strip()
        if not text:
            ctx.error("Expected a play.tv or goplay.be URL, or a content UUID")
            return
        client = self.client(ctx)
        try:
            link = client.resolve_url(text)
        except api.GoPlayError as exc:
            ctx.error(str(exc))
            return
        yield from self._open_link(ctx, client, link)

    def _open_link(self, ctx: FlowContext, client: api.GoPlayApi, link: api.DeepLink) -> Iterator[Ask]:
        if link.kind == "library":
            yield from self.library(ctx)
            return
        if link.kind == "page":
            ctx.status(f"Loading the Play {link.slug} page")
            page = client.page(link.slug)
            yield from self._page_flow(ctx, client, page, title=page.title or link.slug)
            return
        if link.kind == "program":
            ctx.status("Loading the Play program")
            yield from self._program_flow(ctx, client, client.program(link.uuid))
            return
        if link.kind in {"video", "shortform"}:
            item = api.CatalogItem(
                id=link.uuid,
                title=link.uuid,
                kind="SHORTFORM" if link.kind == "shortform" else "VIDEO",
            )
            program = None
            if link.program_uuid:
                try:
                    program = client.program(link.program_uuid)
                    item = api.CatalogItem(
                        id=link.uuid,
                        title=program.title,
                        kind=item.kind,
                        subtitle=program.subtitle,
                    )
                except api.GoPlayError:
                    program = None
            yield from self._emit_video(ctx, client, item, program=program, short_form=link.kind == "shortform")
            return
        if link.kind == "live":
            item = api.CatalogItem(id=link.uuid, title=link.uuid, kind="LIVE")
            yield from self._emit_live(ctx, client, item)
            return
        raise api.GoPlayError("Play did not recognise that URL")

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        keyword = str(query or "").strip()
        if not keyword:
            ctx.warn("Search query is empty")
            return
        client = self.client(ctx)
        ctx.status(f"Searching Play for {keyword}")
        items = client.search(keyword)
        if not items:
            ctx.warn(f"Play found nothing for {keyword}")
            return
        yield from self._item_flow(
            ctx,
            client,
            items,
            title=f"Play · {keyword} · {len(items)} results",
        )

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        ctx.status("Loading Play live channels")
        items = client.live_streams()
        if not items:
            ctx.warn("Play returned no live channels")
            return
        yield from self._item_flow(
            ctx,
            client,
            items,
            title=f"Play Live · {len(items)} channels",
        )

    def library(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        while True:
            try:
                slug = yield ctx.pick(
                    "Play library",
                    [Choice(title, page_slug) for page_slug, title in api.LIBRARY_PAGES],
                )
            except Back:
                return
            if not isinstance(slug, str) or not slug:
                return
            ctx.status(f"Loading {slug}")
            page = client.page(slug)
            yield from self._page_flow(ctx, client, page, title=page.title or slug)

    def _page_flow(
        self,
        ctx: FlowContext,
        client: api.GoPlayApi,
        page: api.CatalogPage,
        *,
        title: str,
    ) -> Iterator[Ask]:
        shelves = [catalog for catalog in page.lists if catalog.list_id]
        if not shelves:
            ctx.warn(f"{title} contains no browsable TV lists")
            return
        while True:
            try:
                selected = yield ctx.pick(
                    title,
                    [
                        Choice(
                            catalog.title or f"Recommendations {index}",
                            catalog,
                            detail=(f"{len(catalog.items)} items" if catalog.items else "TV list"),
                        )
                        for index, catalog in enumerate(shelves, 1)
                    ],
                )
            except Back:
                return
            if not isinstance(selected, api.CatalogList):
                return
            catalog = client.complete_list(selected) if selected.list_id else selected
            yield from self._item_flow(
                ctx,
                client,
                catalog.items,
                title=catalog.title or title,
            )

    def _item_flow(
        self,
        ctx: FlowContext,
        client: api.GoPlayApi,
        items: tuple[api.CatalogItem, ...],
        *,
        title: str,
        program: api.Program | None = None,
        multi: bool = False,
    ) -> Iterator[Ask]:
        if not items:
            ctx.warn(f"{title} contains no items")
            return
        while True:
            try:
                selected = yield ctx.pick(
                    title,
                    [
                        Choice(
                            item.title,
                            item,
                            detail=item.detail,
                            tags=(item.type_label,) if item.type_label else (),
                        )
                        for item in items
                    ],
                    multi=multi,
                    hint="space to tick · enter to confirm" if multi else "",
                )
            except Back:
                return
            chosen = [selected] if isinstance(selected, api.CatalogItem) else list(selected or [])
            chosen = [item for item in chosen if isinstance(item, api.CatalogItem)]
            if not chosen:
                continue
            if len(chosen) > 1:
                try:
                    ctx.batch(len(chosen))
                except Back:
                    continue
            delivery_back = False
            for item in chosen:
                try:
                    yield from self._dispatch(ctx, client, item, program=program)
                except Back:
                    delivery_back = True
                    break
            if delivery_back:
                continue

    def _dispatch(
        self,
        ctx: FlowContext,
        client: api.GoPlayApi,
        item: api.CatalogItem,
        *,
        program: api.Program | None = None,
    ) -> Iterator[Ask]:
        if item.is_program:
            ctx.status(f"Loading {item.title}")
            yield from self._program_flow(ctx, client, client.program(item.id))
            return
        if item.is_live:
            yield from self._emit_live(ctx, client, item)
            return
        if item.is_playable:
            yield from self._emit_video(ctx, client, item, program=program, short_form=item.is_shortform)
            return
        if item.is_page:
            slug = item.slug or item.id
            ctx.status(f"Loading {item.title}")
            page = client.page(slug)
            yield from self._page_flow(ctx, client, page, title=item.title or page.title)
            return
        if item.link:
            yield from self.open_url(ctx, item.link)
            return
        raise api.GoPlayError(f"Play item {item.title!r} has no supported TV action")

    def _program_flow(
        self,
        ctx: FlowContext,
        client: api.GoPlayApi,
        program: api.Program,
    ) -> Iterator[Ask]:
        ctx.log(" · ".join(value for value in (program.title, program.brand, program.category) if value), "ok")
        if program.description:
            ctx.log(program.description, "info")
        playlists = list(program.playlists)
        if program.is_movie and program.next_video_uuid and not playlists:
            item = api.CatalogItem(id=program.next_video_uuid, title=program.title, kind="VIDEO")
            yield from self._emit_video(ctx, client, item, program=program)
            return
        if not playlists:
            extras: list[api.CatalogItem] = []
            if program.next_video_uuid:
                extras.append(api.CatalogItem(id=program.next_video_uuid, title=program.title, kind="VIDEO"))
            if program.trailer_uuid:
                extras.append(
                    api.CatalogItem(id=program.trailer_uuid, title=f"{program.title} trailer", kind="TRAILER")
                )
            if program.teaser_uuid:
                extras.append(api.CatalogItem(id=program.teaser_uuid, title=f"{program.title} teaser", kind="VIDEO"))
            if extras:
                yield from self._item_flow(
                    ctx,
                    client,
                    tuple(extras),
                    title=program.title,
                    program=program,
                )
                return
            raise api.GoPlayError(f"Play program {program.title!r} has no playable TV playlists")

        while True:
            if len(playlists) == 1:
                playlist = playlists[0]
            else:
                try:
                    playlist = yield ctx.pick(
                        program.title,
                        [Choice(value.title, value) for value in playlists],
                    )
                except Back:
                    return
                if not isinstance(playlist, api.Playlist):
                    return
            ctx.status(f"Loading {program.title} · {playlist.title}")
            items = client.complete_playlist(playlist)
            yield from self._item_flow(
                ctx,
                client,
                items,
                title=f"{program.title} · {playlist.title} · {len(items)} items",
                program=program,
                multi=True,
            )
            if len(playlists) == 1:
                return

    # --------------------------------------------------------------- playback
    def _emit_video(
        self,
        ctx: FlowContext,
        client: api.GoPlayApi,
        item: api.CatalogItem,
        *,
        program: api.Program | None = None,
        short_form: bool = False,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading Play video metadata for {item.title}")
        video = client.short_form(item.id) if short_form or item.is_shortform else client.long_form(item.id)
        if program is None and video.program_id:
            try:
                program = client.program(video.program_id)
            except api.GoPlayError:
                program = None
        ctx.status(f"Authorizing Play playback for {video.title or item.title}")
        source = client.play(video)
        title = self._title(item, video, source, program=program, live=False)
        yield ctx.emit(self._playback(title, source, live=False))

    def _emit_live(
        self,
        ctx: FlowContext,
        client: api.GoPlayApi,
        item: api.CatalogItem,
    ) -> Iterator[Ask]:
        ctx.status(f"Authorizing Play live playback for {item.title}")
        card, source = client.live_detail(item.id)
        video = api.Video(
            id=card.id,
            title=source.title or card.now_title or card.title,
            season=card.season,
            episode=card.episode,
            brand=card.brand,
        )
        title = self._title(card, video, source, program=None, live=True)
        yield ctx.emit(self._playback(title, source, live=True))

    def _playback(self, title: Title, source: api.PlaybackSource, *, live: bool) -> Playback:
        drm = None
        if source.drm_xml:
            drm = DrmInfo(
                system="widevine",
                license_url=source.license_url,
                headers=api.drm_headers(source.drm_xml),
                context={"drm_xml": source.drm_xml},
            )
        note = (
            "KeyOS Widevine MPEG-DASH · Play Android TV playback"
            if drm is not None
            else f"clear {source.manifest_type} · Play Android TV playback"
        )
        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest_url,
            alternate_manifest_urls=source.alternate_manifest_urls,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=live,
            drm=drm,
            note=note,
            chapters=list(source.chapters) if not live and self.fetch_chapters_enabled() else [],
        )

    def _title(
        self,
        item: api.CatalogItem,
        video: api.Video,
        source: api.PlaybackSource,
        *,
        program: api.Program | None,
        live: bool,
    ) -> Title:
        name = video.title or source.title or item.title
        if live:
            channel = item.title or item.brand or name
            now = item.now_title or name
            starts = datetime.fromtimestamp(item.starts_at) if item.starts_at else datetime.now()
            ends = datetime.fromtimestamp(item.ends_at) if item.ends_at else None
            scheduled = bool(item.now_title and item.now_title != item.title)
            return Title(
                id=item.id or video.id,
                kind=TitleKind.PROGRAM if scheduled else TitleKind.CHANNEL,
                name=now,
                episode_name=item.now_episode or (now if scheduled else None),
                channel=channel,
                season=item.season if scheduled else None,
                episode=item.episode if scheduled else None,
                starts_at=starts,
                ends_at=ends,
                synopsis=item.description or None,
                cover_url=item.image_url or None,
                service=self.ID,
                data={"video_id": video.id or item.id},
            )

        season = video.season if video.season is not None else item.season
        episode = video.episode if video.episode is not None else item.episode
        series = program.title if program else (video.program_title or name)
        if item.kind == "TRAILER":
            kind = TitleKind.EXTRA
        elif video.short_form or item.is_shortform:
            kind = TitleKind.CLIP
        elif program is not None and program.is_movie and season is None and episode is None:
            kind = TitleKind.MOVIE
        elif season is None and episode is None and program is None:
            kind = TitleKind.MOVIE
        else:
            kind = TitleKind.EPISODE

        if kind is TitleKind.MOVIE:
            return Title(
                id=video.id or item.id,
                kind=TitleKind.MOVIE,
                name=series,
                year=program.year if program else None,
                duration=source.duration or video.duration or item.duration,
                service=self.ID,
                data={"video_id": video.id or item.id},
            )
        return Title(
            id=video.id or item.id,
            kind=kind,
            name=series,
            season=season,
            episode=episode,
            episode_name=name,
            duration=source.duration or video.duration or item.duration,
            service=self.ID,
            data={"video_id": video.id or item.id},
        )

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        license_url = drm.license_url or api.DRM_LICENSE_URL
        if not license_url.startswith("https://drm.play.tv"):
            raise CdmError("Play playback has no valid KeyOS Widevine licence endpoint")
        drm_xml = str(drm.context.get("drm_xml") or drm.headers.get("customdata") or "")
        headers = dict(drm.headers)
        if drm_xml:
            headers.update(api.drm_headers(drm_xml))
        if "customdata" not in headers:
            raise CdmError("Play playback is missing KeyOS licence custom data")
        session = self.ctx.session(user_agent=api.USER_AGENT, cookies=False)
        try:
            response = session.post(
                license_url,
                data=challenge,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise CdmError(f"Play Widevine licence request failed: {exc}") from exc
        if response.status_code >= 400:
            raise CdmError(f"Play Widevine licence request was refused (HTTP {response.status_code})")
        if not response.content:
            raise CdmError("Play Widevine licence response was empty")
        return bytes(response.content)


__all__ = ["GoPlay"]

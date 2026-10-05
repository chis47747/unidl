"""VRT MAX TV-code login, TV catalogue, search, live and Widevine playback.

Authorization: VRT MAX TV code with refresh-token reuse.
Geofence:      Belgium; catalogue visibility and playback depend on location.
Catalogue:     Android TV GraphQL home, program, search and live operations.
Playback:      VRT media-services DASH/HLS; protected video uses Widevine DASH.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import requests

from ...core.cdm import CdmError
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


@registry.register
class VrtMax(Service):
    ID = "vrtmax"
    NAME = "VRT MAX"
    MEDIA_TYPES = ("audio", "video")
    TAG = "VRTMAX"
    ALIASES = ("vrt", "vrt max", "vrtmax")
    TITLE_RE = r"(?:www\.)?vrt\.be/vrtmax/"
    GEOFENCE = ("BE",)
    DESCRIPTION = (
        "VRT MAX TV: TV-code sign-in, TV homepage, URLs, search, live, "
        "programs and Widevine playback."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    SUPPORTS_LIBRARY = False

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._api: api.VrtMaxApi | None = None

    # ------------------------------------------------------------------- auth
    def _save_state(self, state: api.Session) -> None:
        path = self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())
        path.chmod(0o600)

    def _new_client(self, state: api.Session | None = None) -> api.VrtMaxApi:
        return api.VrtMaxApi(
            self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            state or api.Session(),
            on_save=self._save_state,
        )

    def auth_status(self) -> AuthStatus:
        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if not state.signed_in:
            return AuthStatus(False, "TV device is not signed in")
        label = state.account_label or "VRT MAX account"
        if state.is_fresh():
            return AuthStatus(True, label, detail="TV token cache")
        if state.recoverable:
            return AuthStatus(True, f"{label} · refresh required", detail="TV token cache")
        return AuthStatus(False, "TV session expired", detail="Sign in with a new TV code")

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        try:
            ctx.status("Requesting a VRT MAX TV code")
            challenge = client.start_device_code()
        except api.VrtMaxError as exc:
            ctx.error(str(exc))
            return

        lines: list[str | tuple[str, str]] = [
            ("Code", challenge.user_code),
            ("Open", challenge.verification_uri),
        ]
        if challenge.verification_uri_complete:
            lines.append(("Or open with the code filled in", challenge.verification_uri_complete))
        try:
            state = yield ctx.wait_for(
                "Sign in to VRT MAX on another device",
                lines,
                poll=lambda: client.poll_device_code(challenge),
                timeout=float(challenge.expires_in),
                interval=float(challenge.interval),
                hint="Open the page, enter the TV code, then return here.",
            )
        except api.VrtMaxError as exc:
            ctx.error(str(exc))
            return
        if not isinstance(state, api.Session) or not state.is_fresh():
            ctx.error("VRT MAX TV login finished without a usable session")
            return
        self._api = client
        ctx.log(f"signed in · {state.account_label or 'VRT MAX account'}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None
        super().logout()

    def client(self, ctx: FlowContext | None = None) -> api.VrtMaxApi:
        if self._api is not None:
            self._api.ensure_auth()
            self._save_state(self._api.state)
            return self._api

        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if not state.signed_in:
            raise api.AuthenticationRequired(
                "VRT MAX is not signed in. Open Sign in and enter the TV code first."
            )
        client = self._new_client(state)
        if ctx:
            ctx.status("Opening the VRT MAX TV session")
        client.ensure_auth()
        self._save_state(client.state)
        self._api = client
        if ctx:
            ctx.log("VRT MAX TV session ready", "ok")
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
                    Choice("Browse VRT MAX", "browse", detail="Android TV homepage recommendations"),
                    Choice("VOD - open a VRT MAX URL", "url"),
                    Choice("Live TV", "live"),
                    Choice("Search", "search"),
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
                    target = yield ctx.text("Enter a VRT MAX URL")
                    yield from self.open_url(ctx, str(target or "").strip())
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "search":
                    query = yield ctx.text("Search VRT MAX")
                    yield from self.search(ctx, str(query or "").strip())
                elif action == "login":
                    yield from self.begin_login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("signed out", "ok")
                elif action == "settings":
                    yield ctx.settings_request()
            except Back:
                continue
            except RuntimeError as exc:
                ctx.problem(
                    f"{self.NAME} could not finish that",
                    f"{type(exc).__name__}: {exc}",
                    "The service menu is still open - the rest of the session is fine.",
                )

    # --------------------------------------------------------------- catalogue
    def _browse(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        ctx.status("Loading the VRT MAX TV homepage")
        page = client.dynamic_page("/vrtmax/")
        yield from self._page_flow(ctx, client, page, title="VRT MAX TV home")

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        page_id = api.parse_reference(target)
        if page_id is None:
            ctx.error("Expected a https://www.vrt.be/vrtmax/... URL")
            return
        client = self.client(ctx)
        if "/livestream/" in page_id or self._looks_like_episode(page_id):
            item = api.CatalogItem(
                id=page_id,
                object_id="",
                component_id="",
                typename="LivestreamTile" if "/livestream/" in page_id else "EpisodeTile",
                tile_type="livestream" if "/livestream/" in page_id else "episode",
                title=page_id.rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
                description="",
                link=page_id,
                internal_target="livestreampage" if "/livestream/" in page_id else "episodepage",
            )
            yield from self._emit_item(ctx, client, item)
            return
        if self._looks_like_program(page_id):
            ctx.status("Loading the VRT MAX program")
            yield from self._program_flow(ctx, client, client.program(page_id))
            return
        ctx.status("Loading the VRT MAX page")
        page = client.dynamic_page(page_id)
        yield from self._page_flow(ctx, client, page, title=page.title or "VRT MAX")

    @staticmethod
    def _looks_like_program(page_id: str) -> bool:
        parts = [part for part in page_id.split("/") if part]
        return len(parts) == 3 and parts[:2] == ["vrtmax", "a-z"]

    @staticmethod
    def _looks_like_episode(page_id: str) -> bool:
        parts = [part for part in page_id.split("/") if part]
        return len(parts) > 3 and parts[:2] == ["vrtmax", "a-z"]

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        keyword = str(query or "").strip()
        if not keyword:
            ctx.warn("Search query is empty")
            return
        client = self.client(ctx)
        ctx.status(f"Searching VRT MAX for {keyword}")
        items = client.search(keyword)
        if not items:
            ctx.warn(f"VRT MAX found nothing for {keyword}")
            return
        yield from self._item_flow(
            ctx,
            client,
            items,
            title=f"VRT MAX · {keyword} · {len(items)} results",
        )

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        ctx.status("Loading VRT MAX live channels")
        page = client.dynamic_page("livestreams")
        items: list[api.CatalogItem] = []
        for catalog in page.lists:
            complete = client.complete_list(catalog) if catalog.list_id else catalog
            items.extend(item for item in complete.items if item.is_live)
        if not items:
            ctx.warn("VRT MAX returned no live channels")
            return
        yield from self._item_flow(
            ctx,
            client,
            tuple(items),
            title=f"VRT MAX Live · {len(items)} channels",
        )

    def _page_flow(
        self,
        ctx: FlowContext,
        client: api.VrtMaxApi,
        page: api.CatalogPage,
        *,
        title: str,
    ) -> Iterator[Ask]:
        shelves = [catalog for catalog in page.lists if catalog.items or catalog.list_id or catalog.component_id]
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
            catalog = self._resolve_list(client, selected)
            yield from self._item_flow(
                ctx,
                client,
                catalog.items,
                title=catalog.title or title,
            )

    def _item_flow(
        self,
        ctx: FlowContext,
        client: api.VrtMaxApi,
        items: tuple[api.CatalogItem, ...],
        *,
        title: str,
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
                            disabled=not item.available or not item.link,
                        )
                        for item in items
                    ],
                )
            except Back:
                return
            if not isinstance(selected, api.CatalogItem):
                return
            try:
                yield from self._dispatch(ctx, client, selected)
            except Back:
                continue

    def _dispatch(
        self,
        ctx: FlowContext,
        client: api.VrtMaxApi,
        item: api.CatalogItem,
    ) -> Iterator[Ask]:
        if item.is_program:
            ctx.status(f"Loading {item.title}")
            yield from self._program_flow(ctx, client, client.program(item.link))
            return
        if item.is_playable:
            yield from self._emit_item(ctx, client, item)
            return
        if item.link:
            ctx.status(f"Loading {item.title}")
            page = client.dynamic_page(item.link)
            yield from self._page_flow(ctx, client, page, title=item.title)
            return
        raise api.VrtMaxError(f"VRT MAX item {item.title!r} has no supported TV action")

    def _program_flow(
        self,
        ctx: FlowContext,
        client: api.VrtMaxApi,
        program: api.Program,
    ) -> Iterator[Ask]:
        ctx.log(" · ".join(value for value in (program.title, *program.secondary_meta) if value), "ok")
        if program.description:
            ctx.log(program.description, "info")
        sections = list(program.sections)
        if not sections:
            raise api.VrtMaxError(f"VRT MAX program {program.title!r} has no playable TV sections")

        while True:
            if len(sections) == 1:
                section = sections[0]
            else:
                try:
                    section = yield ctx.pick(
                        program.title,
                        [Choice(value.title, value) for value in sections],
                    )
                except Back:
                    return
                if not isinstance(section, api.ProgramSection):
                    return
            yield from self._section_flow(ctx, client, program, section)
            if len(sections) == 1:
                return

    def _section_flow(
        self,
        ctx: FlowContext,
        client: api.VrtMaxApi,
        program: api.Program,
        section: api.ProgramSection,
    ) -> Iterator[Ask]:
        lists = list(section.lists)
        if not lists and section.component_id:
            lists.extend(client.component_lists(section.component_id))
        expanded: list[api.CatalogList] = []
        for catalog in lists:
            if catalog.component_id and not catalog.list_id:
                loaded = client.component_lists(catalog.component_id)
                expanded.extend(
                    value if value.title else api.CatalogList(catalog.title, value.list_id, value.items, value.end_cursor, value.has_next, value.component_id)
                    for value in loaded
                )
            else:
                expanded.append(catalog)
        lists = expanded
        if not lists:
            ctx.warn(f"{program.title} · {section.title} contains no playable lists")
            return

        while True:
            if len(lists) == 1:
                selected_list = lists[0]
            else:
                try:
                    selected_list = yield ctx.pick(
                        f"{program.title} · {section.title}",
                        [
                            Choice(
                                catalog.title or f"List {index}",
                                catalog,
                                detail=(f"{len(catalog.items)} items" if catalog.items else "TV list"),
                            )
                            for index, catalog in enumerate(lists, 1)
                        ],
                    )
                except Back:
                    return
                if not isinstance(selected_list, api.CatalogList):
                    return

            complete = self._resolve_list(client, selected_list)
            while True:
                try:
                    selected = yield ctx.pick(
                        f"{program.title} · {complete.title or section.title} · {len(complete.items)} items",
                        [
                            Choice(
                                item.title,
                                item,
                                detail=item.detail,
                                tags=(item.type_label,) if item.type_label else (),
                                disabled=not item.available or not item.link,
                            )
                            for item in complete.items
                        ],
                        multi=True,
                        hint="space to tick · enter to confirm",
                    )
                except Back:
                    break
                chosen = list(selected or [])
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
                        yield from self._emit_item(ctx, client, item, program=program)
                    except Back:
                        delivery_back = True
                        break
                if delivery_back:
                    continue
            if len(lists) == 1:
                return

    @staticmethod
    def _resolve_list(client: api.VrtMaxApi, catalog: api.CatalogList) -> api.CatalogList:
        if catalog.component_id and not catalog.list_id:
            loaded = client.component_lists(catalog.component_id)
            if not loaded:
                raise api.VrtMaxError(
                    f"VRT MAX component {catalog.component_id!r} contains no TV lists"
                )
            items: list[api.CatalogItem] = []
            for value in loaded:
                complete = client.complete_list(value) if value.list_id else value
                items.extend(complete.items)
            return api.CatalogList(catalog.title, "", tuple(items))
        if catalog.list_id and (catalog.has_next or not catalog.items):
            return client.complete_list(catalog)
        return catalog

    # --------------------------------------------------------------- playback
    def _emit_item(
        self,
        ctx: FlowContext,
        client: api.VrtMaxApi,
        item: api.CatalogItem,
        *,
        program: api.Program | None = None,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading VRT MAX playback metadata for {item.title}")
        player = client.player_data(item.link)
        mode = player.active_mode()
        audio = item.is_audio or player.page_type == "AudioLivestreamPage" or mode.typename.startswith("Audio")
        live = item.is_live or player.page_type in {"LivestreamPage", "AudioLivestreamPage"}
        ctx.status(f"Authorizing VRT MAX playback for {player.title or item.title}")
        source = client.playback_source(mode.stream_id, audio_live=audio and live)
        title = self._title(item, player, source, program=program, live=live, audio=audio)
        drm = (
            DrmInfo(system="widevine", license_url=source.license_url)
            if source.drm_token
            else None
        )
        note = (
            "Widevine MPEG-DASH · VRT MAX Android TV playback"
            if drm is not None
            else f"clear {source.manifest_type} · VRT MAX Android TV playback"
        )
        yield ctx.emit(
            Playback(
                title=title,
                save_name=self.save_name(title),
                manifest_url=source.manifest_url,
                alternate_manifest_urls=source.alternate_manifest_urls,
                headers={"User-Agent": api.USER_AGENT},
                proxy=self.ctx.proxy,
                is_live=live,
                drm=drm,
                note=note,
                audio_only=audio,
                audio_tags=title.audio_tags() if audio else {},
            )
        )

    def _title(
        self,
        item: api.CatalogItem,
        player: api.PlayerData,
        source: api.PlaybackSource,
        *,
        program: api.Program | None,
        live: bool,
        audio: bool,
    ) -> Title:
        name = player.title or source.title or item.title
        if live:
            kind = TitleKind.STATION if audio else TitleKind.CHANNEL
            return Title(
                id=item.id or item.link,
                kind=kind,
                name=name,
                channel=source.channel_id or player.brand or name,
                starts_at=datetime.now(),
                synopsis=source.description or item.description or None,
                cover_url=item.image_url or None,
                service=self.ID,
                data={"page_id": item.link, "stream_id": player.active_mode().stream_id},
            )

        season, episode = api.parse_season_episode(item)
        if audio:
            return Title(
                id=item.id or item.link,
                kind=TitleKind.TRACK,
                name=program.title if program else (player.subtitle or name),
                episode=episode,
                episode_name=name,
                artist=player.brand or None,
                album=program.title if program else (player.subtitle or None),
                synopsis=source.description or item.description or None,
                cover_url=item.image_url or None,
                service=self.ID,
                data={"page_id": item.link, "stream_id": player.active_mode().stream_id},
            )
        if program is not None and program.is_movie and season is None and episode is None:
            return Title(
                id=item.id or item.link,
                kind=TitleKind.MOVIE,
                name=program.title,
                year=program.release_year or player.release_year or item.release_year,
                duration=source.duration,
                service=self.ID,
                data={"page_id": item.link, "stream_id": player.active_mode().stream_id},
            )
        return Title(
            id=item.id or item.link,
            kind=TitleKind.EPISODE,
            name=program.title if program else (player.subtitle or name),
            season=season,
            episode=episode,
            episode_name=name,
            duration=source.duration,
            service=self.ID,
            data={"page_id": item.link, "stream_id": player.active_mode().stream_id},
        )

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        if not drm.license_url.startswith(
            "https://widevine-proxy.drm.technology/proxy?token="
        ):
            raise CdmError("VRT MAX playback has no valid Widevine licence endpoint")
        session = self.ctx.session(user_agent=api.USER_AGENT, cookies=False)
        headers = {"Content-Type": "application/octet-stream", **drm.headers}
        try:
            response = session.post(
                drm.license_url,
                data=challenge,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise CdmError(f"VRT MAX Widevine licence request failed: {exc}") from exc
        if response.status_code >= 400:
            raise CdmError(
                f"VRT MAX Widevine licence request was refused (HTTP {response.status_code})"
            )
        if not response.content:
            raise CdmError("VRT MAX Widevine licence response was empty")
        return bytes(response.content)


__all__ = ["VrtMax"]

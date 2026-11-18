"""ICI TOU.TV - Radio-Canada French OTT."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime

from ...core.cdm import CdmError
from ...core.credentials import mask_account
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext, Pick
from ...core.playback import DrmInfo, Playback
from ...core.qr import QrPresentation
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


@registry.register
class ICITouTV(Service):

    ID = "icitoutv"
    NAME = "ICI TOU.TV"
    TAG = "TOUTV"
    ALIASES = ("toutv", "tou.tv", "ici", "icitoutv", "ici.tou.tv", "tou-tv")
    TITLE_RE = r"(?:www\.)?ici\.tou\.tv/"
    DESCRIPTION = (
        "ICI TOU.TV: Radio-Canada OTT TV code login, search, show URLs, live streams, "
        "DASH + Widevine/PlayReady (x-dt-auth-token)."
    )
    TENANT = api.TOUTV
    API = api
    TOKEN_FILE = api.TOKEN_FILE
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine", "playready")
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    GEOFENCE = ("CA",)

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.RadioCanadaApi | None = None

    def auth_status(self) -> AuthStatus:
        module = self.API
        state = module.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        who = mask_account(state.username) if state.username else self.TENANT.label
        if state.usable():
            detail = "TV code session" if state.tv_code else "existing session · pair again when it expires"
            if not state.claims_usable():
                detail += " · claims refresh required"
            return AuthStatus(True, who, detail=detail, anonymous_ok=True)
        if state.refreshable():
            return AuthStatus(True, who, detail="TV session refresh required", anonymous_ok=True)
        return AuthStatus(
            False,
            "sign in with a TV code",
            detail="live channels remain available",
            anonymous_ok=True,
            logout_available=bool(state.access_token or state.refresh_token or state.claims_token),
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client(for_login=True)
        try:
            ctx.status(f"Requesting a {self.NAME} TV code")
            code = client.start_login()
        except self.API.RadioCanadaError as exc:
            ctx.error(str(exc))
            return

        def poll():
            try:
                return client.poll_login(code)
            except self.API.RadioCanadaError as exc:
                return exc

        open_url = code.direct_url or code.verification_url
        lines: list[str | tuple[str, str]] = [("Open", open_url), ("TV code", code.user_code)]
        if code.direct_url and code.direct_url != code.verification_url:
            lines.append(("Enter code manually", code.verification_url))
        lines.append("Confirm on the official page. UniDL will continue automatically.")
        grant = yield ctx.wait_for(
            f"Sign in to {self.NAME} with a TV code",
            lines,
            poll=poll,
            timeout=code.expires_in,
            interval=code.interval,
            qr=QrPresentation(payload=open_url),
        )
        if isinstance(grant, self.API.RadioCanadaError):
            ctx.error(str(grant))
            return
        if not grant:
            return
        try:
            client.complete_login(grant)
        except self.API.RadioCanadaError as exc:
            ctx.error(str(exc))
            return
        self._api = client
        ctx.log(f"{self.NAME} TV code sign-in completed", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None
        super().logout()

    def _save_state(self, state: api.SessionState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def _new_client(self, *, for_login: bool = False) -> api.RadioCanadaApi:
        module = self.API
        return module.RadioCanadaApi(
            self.ctx.session(user_agent=module.USER_AGENT, cookies=False),
            self.TENANT,
            state=None if for_login else module.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE)),
            on_save=self._save_state,
        )

    def client(self, ctx: FlowContext | None = None) -> api.RadioCanadaApi:
        client = self._public_client()
        if ctx is not None:
            ctx.status(f"Checking {self.NAME} session")
        client.ensure_session()
        return client

    def _licence_client(self) -> api.RadioCanadaApi:
        if self._api is None:
            self._api = self._new_client()
        return self._api

    @staticmethod
    def _licence_params(drm: DrmInfo) -> dict[str, str]:
        context = dict(drm.context or {})
        return {
            "license_url": str(context.get("license_url") or drm.license_url or ""),
            "auth_token": str(context.get("auth_token") or (drm.headers or {}).get("x-dt-auth-token") or ""),
        }

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        try:
            return self._licence_client().widevine_license(challenge, **self._licence_params(drm))
        except api.RadioCanadaError as exc:
            raise CdmError(str(exc)) from exc

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        try:
            return self._licence_client().playready_license(challenge, **self._licence_params(drm))
        except api.RadioCanadaError as exc:
            raise CdmError(str(exc)) from exc

    def _public_client(self) -> api.RadioCanadaApi:
        if self._api is None:
            self._api = self._new_client()
        return self._api

    def _stream(self, client: api.RadioCanadaApi, media_id: str, **kwargs) -> api.Source:
        kwargs["fetch_chapters"] = not kwargs.get("is_live") and self.fetch_chapters_enabled()
        kwargs["fetch_attachments"] = self.fetch_attachments_enabled()
        kwargs["drm_system"] = self.drm_system()
        return client.stream(media_id, **kwargs)

    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        selected = ""
        while True:
            status = self._auth_status_quietly()
            options = [
                Choice("VOD - open a URL or content ID", "url"),
                Choice("Live TV", "live"),
                Choice("Search", "search"),
                Choice("Settings", "settings"),
                Choice("Sign in again with TV code" if status.logged_in else "Sign in with TV code", "login", detail=status.label),
            ]
            if status.logged_in or status.logout_available:
                options.append(Choice("Sign out", "logout", detail=status.label))
            cursor = next((i for i, option in enumerate(options) if option.value == selected), 0)
            selected = yield ctx.pick("", options, cursor=cursor, scope=SCOPE_ROOT)
            try:
                if selected == "url":
                    yield from self._prompt(ctx)
                elif selected == "search":
                    yield from self._prompt(ctx, search=True)
                elif selected == "live":
                    yield from self.live(ctx)
                elif selected == "settings":
                    yield ctx.settings_request()
                elif selected == "login":
                    yield from self.begin_login(ctx)
                elif selected == "logout":
                    self.logout()
                    ctx.log("signed out", "ok")
            except Back:
                pass
            except (self.API.RadioCanadaError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _prompt(
        self, ctx: FlowContext, target: str | None = None, *, search: bool = False,
    ) -> Iterator[Ask]:
        page = ctx.text("Search" if search else "Enter URL or content ID")
        if target is None:
            target = yield page
        while wanted := str(target or "").strip():
            page.default = wanted
            try:
                if search:
                    yield from self._search_results(ctx, wanted)
                else:
                    yield from self._open_target(ctx, wanted)
            except Back:
                pass
            except (self.API.RadioCanadaError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return
            target = yield page

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        yield from self._prompt(ctx, target)

    def _open_target(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = self.API.parse_input(target, default_tenant=self.TENANT.key)
        if parsed is None:
            ctx.error(
                f"That does not look like a {self.NAME} link: {target}\n"
                f"Expected a {self.TENANT.hosts[0]}/… URL or a bare show slug."
            )
            return
        if parsed.tenant_hint and parsed.tenant_hint != self.TENANT.key:
            other = "ICI TOU.TV" if parsed.tenant_hint == "toutv" else "CBC Gem"
            ctx.error(f"That URL belongs to {other}; open it from that service instead.")
            return
        try:
            client = self._public_client()
            ctx.status(f"Loading {parsed.title_id}")
            if parsed.kind == "live":
                yield from self._emit_live(ctx, client, client.live_event(parsed.title_id))
                return
            if parsed.kind in {"section", "collection", "category"}:
                page = client.catalog(parsed.kind, parsed.title_id)
                yield from self._catalog_page(ctx, client, page)
                return
            show = client.show(
                f"{parsed.title_id}/{parsed.selection}" if parsed.selection else parsed.title_id
            )
        except self.API.RadioCanadaError as exc:
            ctx.error(str(exc))
            return
        episode_match = re.fullmatch(r"s(\d+)e(\d+)", parsed.selection, re.I)
        season_match = re.fullmatch(r"s(\d+)", parsed.selection, re.I)
        if episode_match:
            hit = self.API.SearchHit(
                kind="media",
                title=show.title,
                url=f"{show.id}/{parsed.selection}",
            )
            yield from self._search_media(ctx, client, show, hit)
            return
        if season_match and show.kind != "movie":
            yield from self._series(
                ctx,
                client,
                show,
                preferred_season=int(season_match.group(1)),
            )
            return
        yield from self._dispatch_show(ctx, client, show)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        yield from self._prompt(ctx, query, search=True)

    def _search_results(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            return
        try:
            public = self._public_client()
            ctx.status(f"Searching {self.NAME} for {wanted}")
            result = public.search(wanted)
        except self.API.RadioCanadaError as exc:
            ctx.error(str(exc))
            return
        if not result.hits:
            ctx.warn(f"{self.NAME} found no results for {wanted!r}")
            return
        ctx.log(
            f"search matches · {result.total}; available · {len(result.hits)}",
            "info",
        )
        page = ctx.pick(
            f"{self.NAME} · {wanted}",
            [Choice(item.title, item, detail=item.detail) for item in result.hits],
        )
        while True:
            hit = yield page
            if hit is None:
                return
            page.cursor = result.hits.index(hit)
            try:
                yield from self._catalog_item(ctx, public, hit)
            except Back:
                continue
            except (self.API.RadioCanadaError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _catalog_page(
        self,
        ctx: FlowContext,
        client: api.RadioCanadaApi,
        page: api.CatalogPage,
    ) -> Iterator[Ask]:
        lineups = [lineup for lineup in page.lineups if lineup.items]
        if not lineups:
            ctx.warn(f"{page.title}: no browsable items")
            return
        lineup_page = ctx.pick(page.title, [Choice(item.label, item) for item in lineups])
        item_pages: dict[int, Pick] = {}
        while True:
            lineup = lineups[0] if len(lineups) == 1 else (yield lineup_page)
            if lineup is None:
                return
            index = lineup_page.cursor = lineups.index(lineup)
            if index not in item_pages:
                item_pages[index] = ctx.pick(
                    f"{page.title}  ·  {lineup.title}",
                    [Choice(entry.title, entry, detail=entry.detail) for entry in lineup.items],
                )
            item_page = item_pages[index]
            while True:
                try:
                    item = yield item_page
                except Back:
                    break
                if item is None:
                    break
                item_page.cursor = lineup.items.index(item)
                try:
                    yield from self._catalog_item(ctx, client, item)
                except Back:
                    continue
                except (self.API.RadioCanadaError, OSError) as exc:
                    ctx.error(str(exc))
                if not ctx.interactive:
                    return
            if len(lineups) == 1:
                return

    def _catalog_item(
        self,
        ctx: FlowContext,
        client: api.RadioCanadaApi,
        item: api.SearchHit,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading {item.title}")
        if item.kind == "live":
            yield from self._emit_live(ctx, client, client.live_event(item.show_id))
            return
        if item.kind in {"section", "collection", "category"}:
            page = client.catalog(item.kind, item.show_id)
            yield from self._catalog_page(ctx, client, page)
            return
        selection = item.selection if item.kind in {"season", "media"} else ""
        show = client.show(f"{item.show_id}/{selection}" if selection else item.show_id)
        if item.kind == "media":
            yield from self._search_media(ctx, client, show, item)
            return
        if item.kind == "season":
            match = re.fullmatch(r"s(\d+)", item.selection, re.I)
            yield from self._series(
                ctx,
                client,
                show,
                preferred_season=int(match.group(1)) if match else None,
            )
            return
        yield from self._dispatch_show(ctx, client, show)

    def _dispatch_show(
        self,
        ctx: FlowContext,
        client: api.RadioCanadaApi,
        show: api.Show,
    ) -> Iterator[Ask]:
        if show.kind == "movie":
            if show.movies:
                yield from self._movies(ctx, client, show)
            elif show.extras:
                yield from self._extras(ctx, client, show)
            else:
                self._empty_show(ctx, show)
            return
        if show.seasons:
            yield from self._series(ctx, client, show)
        elif show.extras:
            yield from self._extras(ctx, client, show)
        else:
            self._empty_show(ctx, show)

    def _empty_show(self, ctx: FlowContext, show: api.Show) -> None:
        if show.external_url:
            ctx.warn(
                f"{show.title}: this catalogue entry points to an external site "
                "and has no Radio-Canada playback"
            )
            ctx.log(f"external page · {show.external_url}", "info")
            return
        ctx.warn(f"{show.title}: no playable items")

    def _search_media(
        self,
        ctx: FlowContext,
        client: api.RadioCanadaApi,
        show: api.Show,
        hit: api.SearchHit,
    ) -> Iterator[Ask]:
        season_number, episode_number = hit.season_episode
        episodes = [item for season in show.seasons for item in season.episodes]
        selection = hit.selection.casefold()
        episode = next(
            (item for item in episodes if selection and item.selection == selection),
            None,
        )
        if episode is None and hit.media_id:
            episode = next((item for item in episodes if item.media_id == hit.media_id), None)
        if episode is None and episode_number is not None:
            episode = next(
                (
                    item
                    for season in show.seasons if season.number == season_number
                    for item in season.episodes if item.number == episode_number
                ),
                None,
            )
        if episode is None and not hit.media_id:
            yield from self._dispatch_show(ctx, client, show)
            return
        try:
            label = episode.label if episode is not None else hit.title
            ctx.status(f"Resolving {label}")
            source = self._stream(
                client,
                episode.media_id if episode is not None else hit.media_id,
                title=show.title,
                year=(episode.year if episode is not None else "") or show.year,
                season=(episode.season if episode is not None else None) or season_number,
                episode=(episode.number if episode is not None else None) or episode_number,
                episode_name=episode.title if episode is not None else hit.title,
                series_title=show.title,
                media_type=episode.media_type if episode is not None else "episode",
                duration=episode.duration if episode is not None else None,
                synopsis=(episode.synopsis if episode is not None else hit.synopsis)
                or show.synopsis,
                cover_url=(episode.cover_url if episode is not None else hit.cover_url)
                or show.cover_url,
                attachments=(episode.attachments if episode is not None else hit.attachments)
                + show.attachments,
                app_code=(
                    self.API.LIVE_APP_CODE
                    if episode is not None
                    and episode.media_type.lower() == "livetovod"
                    else None
                ),
            )
        except self.API.RadioCanadaError as exc:
            ctx.error(f"{label}: {exc}")
            return
        yield from self._emit(ctx, client, source)

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self._public_client()
            ctx.status(f"Loading {self.NAME} live streams")
            categories = client.live_catalog()
        except self.API.RadioCanadaError as exc:
            ctx.error(str(exc))
            return
        if not categories:
            ctx.warn(f"{self.NAME} returned no live streams")
            return
        category_page = ctx.pick(
            f"{self.NAME} Live",
            [Choice(item.label, item, detail=item.feed_type) for item in categories],
        )
        channel_pages: dict[int, Pick] = {}
        while True:
            category = yield category_page
            if category is None:
                return
            index = category_page.cursor = categories.index(category)
            if index not in channel_pages:
                channel_pages[index] = ctx.pick(
                    f"{self.NAME} Live  ·  {category.title}",
                    [Choice(item.label, item, detail=item.menu_detail) for item in category.channels],
                )
            channel_page = channel_pages[index]
            while True:
                try:
                    channel = yield channel_page
                except Back:
                    break
                if channel is None:
                    break
                channel_page.cursor = category.channels.index(channel)
                try:
                    yield from self._emit_live(ctx, client, channel)
                except Back:
                    continue
                except OSError as exc:
                    ctx.error(str(exc))
                if not ctx.interactive:
                    return

    def _movies(
        self, ctx: FlowContext, client: api.RadioCanadaApi, show: api.Show
    ) -> Iterator[Ask]:
        items = list(show.movies)
        if not items:
            ctx.warn(f"{show.title}: no playable items")
            return
        ctx.log(f"{show.title}: movie · {len(items)} item(s)", "ok")
        page = ctx.pick(
            show.title,
            [Choice(item.label, item, detail=item.media_type) for item in items],
            multi=True,
        )
        while True:
            chosen = items if len(items) == 1 else list((yield page) or [])
            if not chosen:
                return
            try:
                self._remember_batch(ctx, page, chosen)
                for item in chosen:
                    try:
                        ctx.status(f"Resolving {item.title}")
                        source = self._stream(
                            client,
                            item.media_id,
                            title=item.title,
                            year=item.year or show.year,
                            media_type=item.media_type,
                            duration=item.duration,
                            synopsis=item.synopsis or show.synopsis,
                            cover_url=item.cover_url or show.cover_url,
                            attachments=item.attachments + show.attachments,
                            is_live=False,
                            app_code=(
                                self.API.LIVE_APP_CODE if item.is_live_to_vod else None
                            ),
                        )
                    except (self.API.RadioCanadaError, OSError) as exc:
                        ctx.error(f"{item.title}: {exc}")
                        continue
                    yield from self._emit(ctx, client, source)
            except Back:
                if len(items) == 1:
                    raise
                continue
            if len(items) == 1 or not ctx.interactive:
                return

    def _extras(
        self, ctx: FlowContext, client: api.RadioCanadaApi, show: api.Show
    ) -> Iterator[Ask]:
        items = list(show.extras)
        if not items:
            self._empty_show(ctx, show)
            return
        page = ctx.pick(
            f"{show.title}  ·  extras",
            [Choice(item.label, item, detail=item.media_type) for item in items],
            multi=True,
        )
        while True:
            chosen = list((yield page) or [])
            if not chosen:
                return
            try:
                self._remember_batch(ctx, page, chosen)
                for item in chosen:
                    try:
                        ctx.status(f"Resolving {item.title}")
                        source = self._stream(
                            client,
                            item.media_id,
                            title=show.title,
                            year=item.year or show.year,
                            episode_name=item.title,
                            media_type=item.media_type or "extra",
                            duration=item.duration,
                            synopsis=item.synopsis or show.synopsis,
                            cover_url=item.cover_url or show.cover_url,
                            attachments=item.attachments + show.attachments,
                        )
                    except (self.API.RadioCanadaError, OSError) as exc:
                        ctx.error(f"{item.title}: {exc}")
                        continue
                    yield from self._emit(ctx, client, source)
            except Back:
                continue
            if not ctx.interactive:
                return

    @staticmethod
    def _remember_batch(
        ctx: FlowContext, page: Pick, chosen: list[api.MovieItem] | list[api.Episode],
    ) -> None:
        page.preselected = [i for i, choice in enumerate(page.choices) if choice.value in chosen]
        if page.preselected:
            page.cursor = page.preselected[0]
        if len(chosen) > 1:
            ctx.batch(len(chosen))

    def _series(
        self,
        ctx: FlowContext,
        client: api.RadioCanadaApi,
        show: api.Show,
        *,
        preferred_season: int | None = None,
    ) -> Iterator[Ask]:
        seasons = [s for s in show.seasons if s.episodes]
        if not seasons:
            ctx.warn(f"{show.title}: no seasons")
            return
        ctx.log(
            f"{show.title}: {sum(len(s.episodes) for s in seasons)} episode(s) "
            f"in {len(seasons)} season(s)",
            "ok",
        )
        initial = next(
            (season for season in seasons if season.number == preferred_season),
            None,
        )
        if initial is not None or len(seasons) == 1:
            season = initial or seasons[0]
            yield from self._episodes(ctx, client, show, season, self._episode_page(ctx, show, season))
            return

        page = ctx.pick(f"{show.title}  ·  seasons", [Choice(s.label, s) for s in seasons])
        episode_pages: dict[int, Pick] = {}
        while True:
            season = yield page
            if season is None:
                return
            index = page.cursor = seasons.index(season)
            if index not in episode_pages:
                episode_pages[index] = self._episode_page(ctx, show, season)
            try:
                yield from self._episodes(ctx, client, show, season, episode_pages[index])
            except Back:
                continue
            if not ctx.interactive:
                return

    @staticmethod
    def _episode_page(ctx: FlowContext, show: api.Show, season: api.Season) -> Pick:
        label = "Specials" if season.number == 0 else f"Season {season.number}"
        return ctx.pick(
            f"{show.title}  ·  {label}",
            [Choice(ep.label, ep) for ep in season.episodes],
            multi=True,
            hint="space to tick, enter to confirm",
        )

    def _episodes(
        self, ctx: FlowContext, client: api.RadioCanadaApi, show: api.Show,
        season: api.Season, page: Pick,
    ) -> Iterator[Ask]:
        while True:
            chosen = list((yield page) or [])
            if not chosen:
                return
            try:
                self._remember_batch(ctx, page, chosen)
                for ep in chosen:
                    try:
                        ctx.status(f"Resolving {ep.label}")
                        source = self._stream(
                            client,
                            ep.media_id,
                            title=show.title,
                            year=ep.year or show.year,
                            season=ep.season or season.number,
                            episode=ep.number or None,
                            episode_name=ep.title,
                            series_title=show.title,
                            media_type=ep.media_type,
                            duration=ep.duration,
                            synopsis=ep.synopsis or show.synopsis,
                            cover_url=ep.cover_url or show.cover_url,
                            attachments=ep.attachments + show.attachments,
                            app_code=(
                                self.API.LIVE_APP_CODE
                                if ep.media_type.lower() == "livetovod" else None
                            ),
                        )
                    except (self.API.RadioCanadaError, OSError) as exc:
                        ctx.error(f"{ep.label}: {exc}")
                        continue
                    yield from self._emit(ctx, client, source)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _emit_live(
        self, ctx: FlowContext, client: api.RadioCanadaApi, channel: api.LiveChannel
    ) -> Iterator[Ask]:
        if channel.feed_type.casefold() == "liveevent" and channel.starts_at is not None:
            if channel.starts_at > datetime.now(channel.starts_at.tzinfo):
                scheduled = channel.starts_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
                ctx.warn(f"{channel.title}: scheduled for {scheduled}")
                return
        try:
            ctx.status(f"Resolving {channel.title}")
            source = self._stream(
                client,
                channel.media_id,
                title=channel.title,
                episode_name=channel.detail,
                media_type=channel.feed_type,
                synopsis=channel.synopsis,
                cover_url=channel.cover_url,
                attachments=channel.attachments,
                starts_at=channel.starts_at,
                ends_at=channel.ends_at,
                is_live=True,
                app_code=self.API.LIVE_APP_CODE,
            )
        except self.API.RadioCanadaError as exc:
            ctx.error(str(exc))
            return
        yield from self._emit(ctx, client, source)

    def _emit(
        self, ctx: FlowContext, client: api.RadioCanadaApi, source: api.Source
    ) -> Iterator[Ask]:
        self._api = client
        title = self._title(source)
        note = source.note()
        ctx.log(f"{title.name}: {note}", "ok")
        if source.protected:
            if not source.license_url or not source.auth_token:
                ctx.error(f"{title.name}: {source.drm_system} validation did not return a licence URL and auth token")
                return
            drm = DrmInfo(
                system=source.drm_system,
                license_url=source.license_url,
                headers={"x-dt-auth-token": source.auth_token},
                context={
                    "license_url": source.license_url,
                    "auth_token": source.auth_token,
                },
            )
        else:
            drm = DrmInfo(clear=True)
        yield ctx.emit(
            Playback(
                title=title,
                save_name=self.save_name(title),
                manifest_url=source.manifest,
                headers={"User-Agent": self.API.USER_AGENT},
                proxy=self.ctx.proxy,
                is_live=source.is_live,
                note=note,
                drm=drm,
                chapters=list(source.chapters),
                attachments=list(source.attachments),
            )
        )

    def _title(self, source: api.Source) -> Title:
        if source.is_live:
            return Title(
                id=source.media_id,
                kind=TitleKind.CHANNEL,
                name=source.title,
                channel=source.title,
                episode_name=source.episode_name or None,
                starts_at=source.starts_at or datetime.now(),
                ends_at=source.ends_at,
                synopsis=source.synopsis or None,
                cover_url=source.cover_url or None,
                service=self.ID,
            )
        if source.media_type.lower() in {"trailer", "extra", "clip"}:
            return Title(
                id=source.media_id,
                kind=TitleKind.EXTRA,
                name=source.title,
                episode_name=source.episode_name or None,
                year=source.year or None,
                duration=source.duration,
                synopsis=source.synopsis or None,
                cover_url=source.cover_url or None,
                service=self.ID,
            )
        if source.season is not None or source.episode is not None:
            return Title(
                id=source.media_id,
                kind=TitleKind.EPISODE,
                name=source.series_title or source.title,
                season=source.season,
                episode=source.episode,
                episode_name=source.episode_name or None,
                year=source.year or None,
                duration=source.duration,
                synopsis=source.synopsis or None,
                cover_url=source.cover_url or None,
                service=self.ID,
            )
        return Title(
            id=source.media_id,
            kind=TitleKind.MOVIE,
            name=source.title,
            year=source.year or None,
            duration=source.duration,
            synopsis=source.synopsis or None,
            cover_url=source.cover_url or None,
            service=self.ID,
        )


__all__ = ["ICITouTV", "api"]

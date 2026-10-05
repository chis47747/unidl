"""CBC Gem: Radio-Canada English OTT.

Authorization:  email/password ROPC + claims token.
Geofence:       Canada.
Playback:       DASH + Widevine (DRMToday ``x-dt-auth-token``).
Catalogue:
    URL         gem.cbc.ca/<slug>
    Search      public catalog v2 search (shows and exact media hits)
    Live        free TV and linear streams
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime

from ...core.cdm import CdmError
from ...core.credentials import CredentialSlot, mask_account
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


@registry.register
class CBCGem(Service):
    """CBC Gem Canada via services.radio-canada.ca."""

    ID = "cbc"
    NAME = "CBC Gem"
    TAG = "GEM"
    ALIASES = ("cbcgem", "gem", "gem.cbc.ca", "cbc.ca")
    TITLE_RE = r"(?:www\.)?gem\.cbc\.ca/"
    DESCRIPTION = (
        "CBC Gem: Radio-Canada OTT account login, search, show URLs, live streams, "
        "DASH + Widevine (x-dt-auth-token)."
    )
    TENANT = api.GEM
    API = api
    TOKEN_FILE = api.GEM.token_file
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    GEOFENCE = ("CA",)
    CREDENTIALS = [CredentialSlot("default", "CBC Gem account")]

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.RadioCanadaApi | None = None

    # -------------------------------------------------------------------- auth

    def auth_status(self) -> AuthStatus:
        module = self.API
        state = module.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        who = mask_account(state.username) if state.username else self.TENANT.label
        detail = "token cache"
        if state.usable():
            if not state.claims_usable():
                detail = "claims refresh required"
            return AuthStatus(True, who, detail=detail)
        if state.refreshable():
            return AuthStatus(True, who, detail="refresh required")
        cred = self.ctx.credential("default")
        if cred.complete:
            return AuthStatus(
                False,
                f"credentials ready · {mask_account(cred.username)}",
                detail="will sign in on demand",
                anonymous_ok=True,
            )
        return AuthStatus(
            False,
            f"no {self.TENANT.label} account configured",
            detail="live channels remain available",
            anonymous_ok=True,
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        cred = yield from self.sign_in_details(
            ctx,
            title=f"Sign in to {self.NAME}",
            username_label=f"{self.NAME} email",
            password_label=f"{self.NAME} password",
        )
        if not cred.complete:
            return
        client = self._new_client()
        try:
            ctx.status(f"Signing in to {self.NAME}")
            client.login(cred.username, cred.password)
        except self.API.RadioCanadaError as exc:
            ctx.error(str(exc))
            return
        self._api = client
        ctx.log(f"signed in as {mask_account(cred.username)}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None
        super().logout()

    def _save_state(self, state: api.SessionState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def _new_client(self) -> api.RadioCanadaApi:
        module = self.API
        return module.RadioCanadaApi(
            self.ctx.session(user_agent=module.USER_AGENT, cookies=False),
            self.TENANT,
            state=module.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE)),
            on_save=self._save_state,
        )

    def client(self, ctx: FlowContext | None = None) -> api.RadioCanadaApi:
        if (
            self._api is not None
            and self._api.state.usable()
            and self._api.state.claims_usable()
        ):
            return self._api
        client = self._new_client()
        cred = self.ctx.credential("default")
        username = cred.username if cred.complete else None
        password = cred.password if cred.complete else None
        if ctx is not None:
            ctx.status(f"Checking {self.NAME} session")
        try:
            client.ensure_session(username=username, password=password)
        except self.API.RadioCanadaError:
            raise
        self._api = client
        return client

    def _licence_client(self) -> api.RadioCanadaApi:
        if self._api is None:
            self._api = self._new_client()
        return self._api

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        context = dict(drm.context or {})
        license_url = str(context.get("license_url") or drm.license_url or "")
        auth_token = str(
            context.get("auth_token")
            or (drm.headers or {}).get("x-dt-auth-token")
            or ""
        )
        try:
            return self._licence_client().widevine_license(
                challenge,
                license_url=license_url,
                auth_token=auth_token,
            )
        except api.RadioCanadaError as exc:
            raise CdmError(str(exc)) from exc

    def _public_client(self) -> api.RadioCanadaApi:
        if self._api is None:
            self._api = self._new_client()
        return self._api

    def _stream(self, client: api.RadioCanadaApi, media_id: str, **kwargs) -> api.Source:
        """Resolve playback while preserving credential fallback after refresh errors."""
        if not kwargs.get("is_live"):
            credential = self.ctx.credential("default")
            if credential.complete:
                kwargs["username"] = credential.username
                kwargs["password"] = credential.password
        return client.stream(media_id, **kwargs)

    # --------------------------------------------------------------- browsing

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
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
            if parsed.kind in {"section", "collection"}:
                page = client.catalog(parsed.kind, parsed.title_id)
                yield from self._catalog_page(ctx, client, page)
                return
            show = client.show(parsed.title_id)
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
            try:
                yield from self._search_media(ctx, client, show, hit)
            except Back:
                if show.kind != "movie":
                    yield from self._series(
                        ctx,
                        client,
                        show,
                        preferred_season=int(episode_match.group(1)),
                    )
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
        while True:
            try:
                hit = yield ctx.pick(
                    f"{self.NAME} · {wanted}",
                    [Choice(item.title, item, detail=item.detail) for item in result.hits],
                )
            except Back:
                return
            try:
                client = self._public_client()
                yield from self._catalog_item(ctx, client, hit)
            except Back:
                continue
            except self.API.RadioCanadaError as exc:
                ctx.error(str(exc))

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
        while True:
            try:
                if len(lineups) == 1:
                    lineup = lineups[0]
                else:
                    lineup = yield ctx.pick(
                        page.title,
                        [Choice(item.label, item) for item in lineups],
                    )
                if lineup is None:
                    return
            except Back:
                return
            while True:
                try:
                    item = yield ctx.pick(
                        f"{page.title}  ·  {lineup.title}",
                        [
                            Choice(entry.title, entry, detail=entry.detail)
                            for entry in lineup.items
                        ],
                    )
                except Back:
                    if len(lineups) == 1:
                        return
                    break
                if item is None:
                    if len(lineups) == 1:
                        return
                    break
                try:
                    yield from self._catalog_item(ctx, client, item)
                except Back:
                    continue

    def _catalog_item(
        self,
        ctx: FlowContext,
        client: api.RadioCanadaApi,
        item: api.SearchHit,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading {item.title}")
        if item.kind in {"section", "collection"}:
            page = client.catalog(item.kind, item.show_id)
            yield from self._catalog_page(ctx, client, page)
            return
        show = client.show(item.show_id)
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
        episode = next(
            (
                item
                for season in show.seasons
                if season_number is None or season.number == season_number
                for item in season.episodes
                if episode_number is not None and item.number == episode_number
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
        while True:
            try:
                category = yield ctx.pick(
                    f"{self.NAME} Live",
                    [
                        Choice(item.label, item, detail=item.feed_type)
                        for item in categories
                    ],
                )
            except Back:
                return
            if category is None:
                return
            while True:
                try:
                    channel = yield ctx.pick(
                        f"{self.NAME} Live  ·  {category.title}",
                        [
                            Choice(item.label, item, detail=item.menu_detail)
                            for item in category.channels
                        ],
                    )
                except Back:
                    break
                if channel is None:
                    break
                try:
                    yield from self._emit_live(ctx, client, channel)
                except Back:
                    continue

    def _movies(
        self, ctx: FlowContext, client: api.RadioCanadaApi, show: api.Show
    ) -> Iterator[Ask]:
        items = list(show.movies)
        if not items:
            ctx.warn(f"{show.title}: no playable items")
            return
        ctx.log(f"{show.title}: movie · {len(items)} item(s)", "ok")
        while True:
            try:
                if len(items) == 1:
                    chosen = items
                else:
                    picked = yield ctx.pick(
                        show.title,
                        [Choice(item.label, item, detail=item.media_type) for item in items],
                        multi=True,
                    )
                    chosen = list(picked or [])
            except Back:
                return
            if not chosen:
                return
            if len(chosen) > 1:
                ctx.batch(len(chosen))
            try:
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
                            is_live=False,
                            app_code=(
                                self.API.LIVE_APP_CODE if item.is_live_to_vod else None
                            ),
                        )
                    except self.API.RadioCanadaError as exc:
                        ctx.error(f"{item.title}: {exc}")
                        continue
                    yield from self._emit(ctx, client, source)
            except Back:
                if len(items) == 1:
                    raise
                continue
            if len(items) == 1:
                return

    def _extras(
        self, ctx: FlowContext, client: api.RadioCanadaApi, show: api.Show
    ) -> Iterator[Ask]:
        items = list(show.extras)
        if not items:
            self._empty_show(ctx, show)
            return
        while True:
            try:
                picked = yield ctx.pick(
                    f"{show.title}  ·  extras",
                    [Choice(item.label, item, detail=item.media_type) for item in items],
                    multi=True,
                )
            except Back:
                return
            chosen = list(picked or [])
            if not chosen:
                return
            if len(chosen) > 1:
                ctx.batch(len(chosen))
            try:
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
                        )
                    except self.API.RadioCanadaError as exc:
                        ctx.error(f"{item.title}: {exc}")
                        continue
                    yield from self._emit(ctx, client, source)
            except Back:
                continue

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
        while True:
            try:
                if initial is not None:
                    season = initial
                    initial = None
                elif len(seasons) == 1:
                    season = seasons[0]
                else:
                    season = yield ctx.pick(
                        f"{show.title}  ·  seasons",
                        [Choice(s.label, s) for s in seasons],
                    )
                    if season is None:
                        return
            except Back:
                return
            while True:
                try:
                    season_label = (
                        "Specials" if season.number == 0 else f"Season {season.number}"
                    )
                    picked = yield ctx.pick(
                        f"{show.title}  ·  {season_label}",
                        [Choice(ep.label, ep) for ep in season.episodes],
                        multi=True,
                        hint="space to tick, enter to confirm",
                    )
                except Back:
                    if len(seasons) == 1:
                        return
                    break
                chosen = list(picked or [])
                if not chosen:
                    if len(seasons) == 1:
                        return
                    break
                if len(chosen) > 1:
                    ctx.batch(len(chosen))
                try:
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
                                app_code=(
                                    self.API.LIVE_APP_CODE
                                    if ep.media_type.lower() == "livetovod"
                                    else None
                                ),
                            )
                        except self.API.RadioCanadaError as exc:
                            ctx.error(f"{ep.label}: {exc}")
                            continue
                        yield from self._emit(ctx, client, source)
                except Back:
                    continue
            if len(seasons) == 1:
                return

    def _emit_live(
        self, ctx: FlowContext, client: api.RadioCanadaApi, channel: api.LiveChannel
    ) -> Iterator[Ask]:
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
                ctx.error(f"{title.name}: DRM stream missing licence URL or auth token")
                return
            drm = DrmInfo(
                system="widevine",
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


__all__ = ["CBCGem", "api"]

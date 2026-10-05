"""NRK TV - catalogue, TV-code or anonymous sign-in, live TV and playback."""

from __future__ import annotations

from collections.abc import Iterator

from ...core.cdm import CdmError
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


@registry.register
class NRK(Service):
    """NRK TV's Android TV catalogue and playback contract."""

    ID = "nrk"
    NAME = "NRK TV"
    TAG = "NRK"
    ALIASES = ("nrk tv", "nrktv", "tv.nrk.no")
    TITLE_RE = r"(?:^|://)(?:www\.)?tv\.nrk\.no/"
    GEOFENCE = ("NO",)
    DESCRIPTION = "NRK TV catalogue, TV-code or anonymous access, live channels and HLS/DASH playback."

    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    SUPPORTS_LIBRARY = True
    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._api: api.NrkApi | None = None

    # ---------------------------------------------------------------- auth
    def _cached_state(self) -> api.SessionState:
        return api.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))

    def _save_state(self, state: api.SessionState) -> None:
        path = self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())
        path.chmod(0o600)

    def _new_client(self) -> api.NrkApi:
        return api.NrkApi(
            self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            state=self._cached_state(),
            on_save=self._save_state,
        )

    def auth_status(self) -> AuthStatus:
        state = self._cached_state()
        if state.signed_in:
            label = state.account_name or "NRK TV account"
            detail = "saved session" if state.is_fresh() else "saved session will refresh on use"
            return AuthStatus(True, label, detail=detail, anonymous_ok=True)
        return AuthStatus(False, "anonymous", detail="public catalogue and playback", anonymous_ok=True)

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        try:
            selection = yield ctx.pick(
                "NRK TV sign-in",
                [
                    Choice("TV code", "tv", detail="Activate this device from another device"),
                    Choice("Continue anonymously", "anonymous", detail="Use public catalogue and playback"),
                ],
            )
        except Back:
            return
        if selection == "anonymous":
            client.use_anonymous()
            self._api = client
            ctx.log("NRK TV anonymous mode enabled", "ok")
            return
        if selection != "tv":
            return

        try:
            ctx.status("Requesting an NRK TV activation code")
            challenge = client.start_device_login()
            confirmed = yield ctx.wait_for(
                "Sign in to NRK TV",
                [
                    ("Code", challenge.user_code),
                    ("Open", challenge.verification_url_complete or challenge.verification_url),
                    "Approve this device on the activation page; this screen checks automatically.",
                ],
                poll=lambda: client.poll_device_login(challenge),
                timeout=float(challenge.expires_in),
                interval=float(challenge.interval),
            )
        except api.NrkError as exc:
            ctx.error(str(exc))
            return
        if not isinstance(confirmed, api.SessionState) or not confirmed.signed_in:
            ctx.error("NRK TV activation completed without a usable session")
            return
        self._api = client
        ctx.log(f"NRK TV sign-in completed · {confirmed.account_name or 'account'}", "ok")

    def logout(self) -> None:
        client = self._api or self._new_client()
        client.revoke()
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None

    def client(self, ctx: FlowContext | None = None) -> api.NrkApi:
        client = self._api
        if client is None:
            client = self._new_client()
            self._api = client
        if client.state.signed_in and not client.state.is_fresh():
            if ctx is not None:
                ctx.status("Refreshing the NRK TV session")
            client.ensure_auth()
        return client

    # --------------------------------------------------------------- top menu
    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        while True:
            status = self._auth_status_quietly()
            choices: list[Choice] = []
            if not status.usable:
                choices.append(Choice("Sign in", "login", detail=status.label))
            choices.extend(
                [
                    Choice("Browse NRK TV", "browse"),
                    Choice("VOD - open a URL or content ID", "url"),
                    Choice("Live TV", "live"),
                    Choice("Search", "search"),
                    Choice("Settings", "settings"),
                ]
            )
            if status.logged_in:
                choices.append(Choice("Sign in", "login", detail="Switch account"))
            else:
                choices.append(Choice("Sign in", "login", detail=status.label))
            if status.logged_in:
                choices.append(Choice("Sign out", "logout", detail=status.label))

            action = yield ctx.pick("", choices, scope=SCOPE_ROOT)
            try:
                if action == "browse":
                    yield from self.library(ctx)
                elif action == "url":
                    while True:
                        target = yield ctx.text("Enter an NRK TV URL or content ID")
                        wanted = str(target or "").strip()
                        if not wanted:
                            break
                        yield from self.open_url(ctx, wanted)
                        if not ctx.interactive:
                            break
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "search":
                    while True:
                        query = yield ctx.text("Search NRK TV")
                        wanted = str(query or "").strip()
                        if not wanted:
                            break
                        yield from self.search(ctx, wanted)
                        if not ctx.interactive:
                            break
                elif action == "login":
                    yield from self.login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("NRK TV signed out", "ok")
                elif action == "settings":
                    yield ctx.settings_request()
            except Back:
                continue
            except api.AuthenticationRequired as exc:
                ctx.error(str(exc))
            except api.NrkError as exc:
                ctx.error(str(exc))

    # -------------------------------------------------------------- catalogue
    def library(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading NRK TV recommendations")
            sections = client.home()
        except api.NrkError as exc:
            ctx.error(str(exc))
            return
        if not sections:
            ctx.warn("NRK TV returned no recommendation sections")
            return
        while True:
            try:
                section = yield ctx.pick(
                    "NRK TV · Home",
                    [Choice(item.title, item, detail=item.display_contract) for item in sections],
                )
            except Back:
                return
            try:
                ctx.status(f"Loading {section.title}")
                items = client.section_items(section)
                yield from self._items_flow(ctx, client, items, section.title)
            except Back:
                continue
            except api.NrkError as exc:
                ctx.error(str(exc))

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(f"That is not an NRK TV URL or content ID: {target}")
            return
        client = self.client(ctx)
        if parsed.kind == "series":
            yield from self._series_flow(ctx, client, parsed.series_id or parsed.id, parsed.season_id)
            return
        if parsed.kind == "channel":
            try:
                ctx.status(f"Resolving NRK TV channel {parsed.id}")
                channels = client.live_channels()
                channel = next((item for item in channels if item.id.casefold() == parsed.id.casefold()), None)
                if channel is None:
                    raise api.NrkError(f"NRK TV did not return live channel {parsed.id}")
                yield from self._emit_channel(ctx, client, channel)
            except api.NrkError as exc:
                ctx.error(str(exc))
            return
        try:
            ctx.status(f"Resolving NRK TV program {parsed.id}")
            item = client.program(parsed.id)
            yield from self._emit_item(ctx, client, item)
        except api.NrkError as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            return
        try:
            client = self.client(ctx)
            ctx.status(f"Searching NRK TV for {wanted}")
            items = client.search(wanted)
        except api.NrkError as exc:
            ctx.error(str(exc))
            return
        yield from self._items_flow(ctx, client, items, f"NRK TV · Search · {wanted}")

    def _items_flow(
        self,
        ctx: FlowContext,
        client: api.NrkApi,
        items: list[api.CatalogItem],
        heading: str,
    ) -> Iterator[Ask]:
        unique: list[api.CatalogItem] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            key = (item.kind, item.id)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        if not unique:
            ctx.warn(f"{heading}: no results")
            return
        while True:
            try:
                selected = yield ctx.pick(
                    heading,
                    [Choice(item.title, item, detail=item.detail, tags=(item.type_label,)) for item in unique],
                )
            except Back:
                return
            try:
                yield from self._dispatch_item(ctx, client, selected)
            except Back:
                continue
            except api.NrkError as exc:
                ctx.error(str(exc))

    def _dispatch_item(self, ctx: FlowContext, client: api.NrkApi, item: api.CatalogItem) -> Iterator[Ask]:
        if item.kind == "series":
            yield from self._series_flow(ctx, client, item.id, "")
        elif item.kind == "channel":
            channel = api.Channel(item.id, item.title, item.description, raw=item.raw)
            yield from self._emit_channel(ctx, client, channel)
        else:
            yield from self._emit_item(ctx, client, item)

    def _series_flow(
        self,
        ctx: FlowContext,
        client: api.NrkApi,
        series_id: str,
        season_id: str,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading NRK TV series {series_id}")
        series = client.series(series_id)
        if season_id:
            seasons = [season for season in series.seasons if season.id == season_id]
            if not seasons:
                seasons = [api.Season(season_id, f"Season {season_id}")]
        else:
            seasons = list(series.seasons)
        if series.latest_episodes:
            seasons.insert(0, api.Season("__latest__", "Latest episodes"))
        if not seasons:
            ctx.warn(f"NRK TV returned no seasons for {series.title}")
            return
        while True:
            if len(seasons) == 1:
                selected_season = seasons[0]
            else:
                try:
                    selected_season = yield ctx.pick(
                        f"{series.title} · Seasons",
                        [Choice(season.title, season) for season in seasons],
                    )
                except Back:
                    return
            if selected_season.id == "__latest__":
                episodes = series.latest_episodes
            else:
                ctx.status(f"Loading {series.title} · {selected_season.title}")
                episodes = client.season(series.id, selected_season.id)
            yield from self._episodes_flow(ctx, client, series, selected_season, episodes)
            if len(seasons) == 1 or not ctx.interactive:
                return

    def _episodes_flow(
        self,
        ctx: FlowContext,
        client: api.NrkApi,
        series: api.Series,
        season: api.Season,
        episodes: list[api.CatalogItem],
    ) -> Iterator[Ask]:
        if not episodes:
            ctx.warn(f"NRK TV returned no episodes for {series.title} · {season.title}")
            return
        for index, episode in enumerate(episodes, start=1):
            episode.series_id = episode.series_id or series.id
            episode.season_id = episode.season_id or season.id
            if not episode.subtitle:
                episode.subtitle = series.title
            episode.raw.setdefault("episode_number", index)
        while True:
            try:
                selected = yield ctx.pick(
                    f"{series.title} · {season.title}",
                    [
                        Choice(
                            episode.title,
                            episode,
                            detail=episode.detail,
                            tags=("episode",),
                        )
                        for episode in episodes
                    ],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                return
            chosen = list(selected or [])
            if not chosen:
                if not ctx.interactive:
                    return
                continue
            try:
                if len(chosen) > 1:
                    ctx.batch(len(chosen))
                for item in chosen:
                    yield from self._emit_item(ctx, client, item, series_title=series.title)
            except Back:
                continue
            if not ctx.interactive:
                return

    # ------------------------------------------------------------- playback
    def _emit_item(
        self,
        ctx: FlowContext,
        client: api.NrkApi,
        item: api.CatalogItem,
        *,
        series_title: str = "",
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving NRK TV playback for {item.title}")
            source = client.playback(item)
        except api.NrkError as exc:
            ctx.error(f"{item.title}: {exc}")
            return
        title = self._title_for_item(item, series_title=series_title)
        yield ctx.emit(self._playback(title, source))

    def _emit_channel(self, ctx: FlowContext, client: api.NrkApi, channel: api.Channel) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving NRK TV live playback for {channel.title}")
            source = client.playback(api.ParsedTarget("channel", channel.id))
        except api.NrkError as exc:
            ctx.error(f"{channel.title}: {exc}")
            return
        title = Title(
            id=channel.id,
            kind=TitleKind.CHANNEL,
            name=channel.title,
            channel=channel.title,
            episode_name=channel.current_title or None,
            synopsis=channel.description or None,
            service=self.ID,
            data={"geo_blocked": channel.geo_blocked},
        )
        yield ctx.emit(self._playback(title, source))

    @staticmethod
    def _title_for_item(item: api.CatalogItem, *, series_title: str = "") -> Title:
        name = series_title or item.subtitle or item.title
        episode_name = item.title if series_title or item.kind == "episode" else None
        kind = TitleKind.EPISODE if item.kind in {"episode", "program", "extra"} else TitleKind.CLIP
        return Title(
            id=item.id,
            kind=kind,
            name=name,
            year=item.production_year or None,
            episode_name=episode_name,
            duration=item.duration,
            synopsis=item.description or None,
            cover_url=item.image_url or None,
            service="nrk",
        )

    def _playback(self, title: Title, source: api.PlaybackSource) -> Playback:
        if source.encrypted and source.encryption_scheme not in {"clearkey", "widevine"}:
            raise api.NrkError(f"NRK returned unsupported encryption scheme {source.encryption_scheme}")
        drm = DrmInfo(clear=True)
        if source.encrypted and source.encryption_scheme == "widevine":
            if not source.license_url:
                raise api.NrkError("NRK Widevine playback returned no licence URL")
            drm = DrmInfo(
                system="widevine",
                license_url=source.license_url,
                headers={
                    "Accept": "application/octet-stream",
                    "Content-Type": "application/octet-stream",
                    "User-Agent": api.USER_AGENT,
                },
            )
        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest_url,
            headers={"User-Agent": api.USER_AGENT, "Android-Caller": api.ANDROID_CALLER},
            proxy=self.ctx.proxy,
            is_live=source.is_live,
            drm=drm,
            keys=list(source.keys),
            note=source.note,
        )

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        if not drm.license_url:
            raise CdmError("NRK playback returned no Widevine licence URL")
        try:
            return (self._api or self._new_client()).widevine_license(drm.license_url, challenge)
        except api.NrkError as exc:
            raise CdmError(str(exc)) from exc

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading NRK TV live channels")
            channels = client.live_channels()
        except api.NrkError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("NRK TV returned no live channels")
            return
        while True:
            try:
                selected = yield ctx.table(
                    f"NRK TV · Live · {len(channels)} channels",
                    ["Channel", "Type", "Availability"],
                    [
                        [
                            channel.title,
                            channel.channel_type,
                            "Norway only" if channel.geo_blocked else "Available",
                        ]
                        for channel in channels
                    ],
                    channels,
                )
            except Back:
                return
            if selected is None:
                return
            try:
                yield from self._emit_channel(ctx, client, selected)
            except Back:
                continue


__all__ = ["NRK", "api"]

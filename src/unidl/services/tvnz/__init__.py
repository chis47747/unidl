"""Native TVNZ+ service: Android TV auth, catalogue, live and Widevine."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

from ...core.cdm import CdmError
from ...core.credentials import CredentialSlot, mask_account
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_LOGIN_METHOD = Setting(
    key="login_method",
    label="Login method",
    kind="choice",
    options=[
        Option("account", "Account - email and password"),
        Option("otp", "Email verification code"),
    ],
    default="account",
    help="OTP codes are entered interactively and are never stored.",
    resets_session=True,
)


@registry.register
class TVNZ(Service):
    ID = "tvnz"
    NAME = "TVNZ+"
    TAG = "TVNZ"
    ALIASES = ("tvnz+", "tvnz.co.nz", "tvnz ondemand")
    TITLE_RE = (
        r"(?:www\.)?tvnz\.co\.nz/(?:player/)?"
        r"(?:tvseries|tvepisode|movie|newsclip|liveevent|event|sporthighlight|sportclip)/"
    )
    GEOFENCE = ("NZ",)
    DESCRIPTION = (
        "TVNZ+ Android TV account or email-code sign-in, VOD, search, live TV, "
        "sports events and Widevine playback."
    )
    USES = Capabilities()
    SETTINGS = [_LOGIN_METHOD]
    CREDENTIALS = [CredentialSlot("default", "TVNZ+ account", optional=True)]
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    SUPPORTS_LIBRARY = False

    TOKEN_FILE = api.TOKEN_FILE
    DEVICE_FILE = api.DEVICE_FILE

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._api: api.TvnzApi | None = None
        self._device_id: str = ""

    def login_method(self) -> str:
        return "otp" if str(self.settings.get("login_method") or "account") == "otp" else "account"

    def device_id(self) -> str:
        if self._device_id:
            return self._device_id
        cached = self.ctx.tokens.read(self.DEVICE_FILE) or {}
        raw = str(cached.get("device_id") or "").strip()
        try:
            value = uuid.UUID(raw).hex if raw else ""
        except ValueError:
            value = ""
        if not value:
            value = uuid.uuid4().hex
            path = self.ctx.tokens.write(self.DEVICE_FILE, {"device_id": value})
            Path(path).chmod(0o600)
        self._device_id = value
        return value

    def _cached_state(self) -> api.SessionState:
        return api.SessionState.from_cache(
            self.ctx.tokens.read(self.TOKEN_FILE),
            device_id=self.device_id(),
        )

    def auth_status(self) -> AuthStatus:
        state = self._cached_state()
        if state.ready:
            return AuthStatus(
                logged_in=True,
                label=f"{state.login_method or 'account'} - {mask_account(state.email)}",
                detail="access and entitlement tokens ready",
            )
        if state.can_refresh:
            return AuthStatus(
                logged_in=True,
                label=f"{state.login_method or 'account'} - refresh ready",
                detail=mask_account(state.email),
            )
        credential = self.ctx.credential("default")
        if credential.username:
            detail = (
                "credentials ready"
                if self.login_method() == "account" and credential.password
                else "email ready"
            )
            return AuthStatus(
                logged_in=False,
                label=f"{self.login_method()} - {mask_account(credential.username)}",
                detail=detail,
            )
        return AuthStatus(
            logged_in=False,
            label=f"{self.login_method()} - sign-in required",
            detail="configure credentials.default or enter the email interactively",
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        method = self.login_method()
        credential = self.ctx.credential("default")
        email = credential.username or str(
            (yield ctx.text("TVNZ+ email", placeholder="name@example.com")) or ""
        ).strip()
        if not email:
            ctx.warn("TVNZ+ email is required")
            return

        client = self._new_client(api.SessionState(device_id=self.device_id()))
        try:
            ctx.status("Checking the TVNZ+ account")
            account = client.account(email)
        except api.TvnzError as exc:
            ctx.error(str(exc))
            return
        ctx.log(f"TVNZ+ account found ({account.account_type or 'active'})", "ok")

        if method == "account":
            password = credential.password or str(
                (yield ctx.text("TVNZ+ password", password=True)) or ""
            )
            if not password:
                ctx.warn("TVNZ+ password is required for account login")
                return
            try:
                ctx.status("Signing in to TVNZ+")
                client.login_password(email, password)
            except api.TvnzError as exc:
                ctx.error(str(exc))
                return
        else:
            try:
                ctx.status("Sending a TVNZ+ verification code")
                challenge = client.create_otp(email)
            except api.TvnzError as exc:
                ctx.error(str(exc))
                return
            if challenge.maximum_attempts:
                ctx.log(
                    "verification code sent "
                    f"({challenge.current_attempt}/{challenge.maximum_attempts})",
                    "ok",
                )
            else:
                ctx.log("verification code sent", "ok")

            while True:
                code = str(
                    (yield ctx.text("TVNZ+ verification code", password=True)) or ""
                ).strip()
                if not code:
                    ctx.warn("Verification code is required")
                    return
                try:
                    ctx.status("Verifying the TVNZ+ code")
                    client.confirm_otp(email, code)
                except api.TvnzError as exc:
                    ctx.error(str(exc))
                    continue
                break

        try:
            ctx.status("Loading TVNZ+ playback entitlements")
            client.refresh_entitlements()
        except api.TvnzError as exc:
            ctx.error(str(exc))
            return
        self._api = client
        ctx.log(f"signed in to TVNZ+ as {mask_account(email)}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        if self._api is not None:
            self._api.clear_session()
        self._api = None
        super().logout()

    def _save_state(self, state: api.SessionState) -> None:
        path = self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())
        Path(path).chmod(0o600)

    def _new_client(self, state: api.SessionState | None = None) -> api.TvnzApi:
        device_id = self.device_id()
        return api.TvnzApi(
            self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            device_id=device_id,
            state=state or api.SessionState.from_cache(
                self.ctx.tokens.read(self.TOKEN_FILE),
                device_id=device_id,
            ),
            on_save=self._save_state,
        )

    def client(self, ctx: FlowContext | None = None) -> api.TvnzApi:
        if self._api is None:
            self._api = self._new_client()
        if ctx is not None:
            ctx.status("Checking TVNZ+ access and entitlement tokens")
        self._api.ensure_auth()
        return self._api

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                "Enter a TVNZ+ player URL for a series, episode, movie, news clip, "
                "sports item or live event"
            )
            return
        try:
            client = self.client(ctx)
            if parsed.kind == "tvseries":
                yield from self._series(ctx, client, parsed.slug)
                return
            ctx.status(f"Loading TVNZ+ {parsed.kind}")
            item = client.content(parsed.kind, parsed.slug)
            yield from self._emit_content(ctx, client, item)
        except api.TvnzError as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            return
        try:
            client = self.client(ctx)
            ctx.status(f"Searching TVNZ+ for {wanted}")
            hits = client.search(wanted)
        except api.TvnzError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"TVNZ+ found nothing for {wanted!r}")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    f"TVNZ+ - {wanted} - {len(hits)} results",
                    [
                        Choice(
                            hit.label,
                            hit,
                            detail=hit.detail,
                            tags=(hit.kind,),
                            disabled=not hit.playable,
                        )
                        for hit in hits
                    ],
                )
            except Back:
                return
            if chosen is None:
                return
            try:
                if chosen.kind == "tvseries":
                    yield from self._series(ctx, client, chosen.slug)
                else:
                    ctx.status(f"Loading {chosen.title}")
                    item = client.content(chosen.kind, chosen.slug)
                    yield from self._emit_content(ctx, client, item)
            except Back:
                continue
            except api.TvnzError as exc:
                ctx.error(str(exc))

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading TVNZ+ live channels")
            channels = client.channels()
        except api.TvnzError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("TVNZ+ returned no live channels")
            return

        while True:
            try:
                chosen = yield ctx.table(
                    f"TVNZ+ Live - {len(channels)} channels",
                    ["Channel", "Now", "Description"],
                    [
                        (channel.name, channel.now or "-", channel.description[:90] or "-")
                        for channel in channels
                    ],
                    channels,
                )
            except Back:
                return
            if chosen is None:
                return
            try:
                yield from self._emit_channel(ctx, client, chosen)
            except Back:
                continue

    def _series(
        self,
        ctx: FlowContext,
        client: api.TvnzApi,
        slug: str,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading TVNZ+ series {slug}")
            series = client.series(slug)
            seasons = client.seasons(series.id)
        except api.TvnzError as exc:
            ctx.error(str(exc))
            return
        if not seasons:
            ctx.warn(f"{series.title} has no seasons")
            return
        ctx.log(f"{series.title}: {len(seasons)} season(s)", "ok")
        episode_cache: dict[str, list[api.Episode]] = {}

        while True:
            try:
                season = yield ctx.pick(
                    f"{series.title} - seasons",
                    [Choice(item.label, item) for item in seasons],
                )
            except Back:
                return
            if season is None:
                return
            try:
                if season.id not in episode_cache:
                    ctx.status(f"Loading {series.title} - {season.label}")
                    episode_cache[season.id] = client.episodes(series, season)
                episodes = episode_cache[season.id]
            except api.TvnzError as exc:
                ctx.error(str(exc))
                continue
            if not episodes:
                ctx.warn(f"{season.label} has no episodes")
                continue
            try:
                yield from self._episode_list(ctx, client, series, season, episodes)
            except Back:
                continue

    def _episode_list(
        self,
        ctx: FlowContext,
        client: api.TvnzApi,
        series: api.Series,
        season: api.Season,
        episodes: list[api.Episode],
    ) -> Iterator[Ask]:
        while True:
            picked = yield ctx.pick(
                f"{series.title} - {season.label} - {len(episodes)} episodes",
                [
                    Choice(
                        episode.label,
                        episode,
                        detail=episode.description[:90],
                    )
                    for episode in episodes
                ],
                multi=True,
                hint="space to tick, enter to confirm",
            )
            chosen = list(picked or [])
            if not chosen:
                return
            if len(chosen) > 1:
                ctx.batch(len(chosen))
            try:
                for episode in chosen:
                    yield from self._emit_episode(ctx, client, episode)
            except Back:
                continue

    def _emit_episode(
        self,
        ctx: FlowContext,
        client: api.TvnzApi,
        episode: api.Episode,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {episode.label}")
            source = client.source(
                episode.content_id,
                catalog_type="tvepisode",
            )
        except api.TvnzError as exc:
            ctx.error(f"{episode.label}: {exc}")
            return
        title = Title(
            id=episode.content_id,
            kind=TitleKind.EPISODE,
            name=episode.series_title,
            season=episode.season,
            episode=episode.number,
            episode_name=episode.name,
            synopsis=episode.description or None,
            service=self.ID,
        )
        ctx.log(f"{episode.label}: {source.line()}")
        yield ctx.emit(self._playback(title, source))

    def _emit_content(
        self,
        ctx: FlowContext,
        client: api.TvnzApi,
        item: api.Content,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {item.title}")
            if item.kind == "liveevent":
                source = client.live_event_source(item)
            else:
                source = client.source(item.content_id, catalog_type=item.kind)
        except api.TvnzError as exc:
            ctx.error(f"{item.title}: {exc}")
            return
        title = self._content_title(item)
        ctx.log(f"{title.full_label()}: {source.line()}")
        yield ctx.emit(self._playback(title, source))

    def _emit_channel(
        self,
        ctx: FlowContext,
        client: api.TvnzApi,
        channel: api.Channel,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {channel.name}")
            source = client.channel_source(channel)
        except api.TvnzError as exc:
            ctx.error(f"{channel.name}: {exc}")
            return
        title = Title(
            id=channel.content_id,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            synopsis=channel.description or None,
            service=self.ID,
        )
        note = f"{source.line()} - now: {channel.now}" if channel.now else source.line()
        ctx.log(f"{channel.name}: {note}")
        yield ctx.emit(self._playback(title, source, note=note))

    def _content_title(self, item: api.Content) -> Title:
        if item.kind == "movie":
            return Title(
                id=item.content_id,
                kind=TitleKind.MOVIE,
                name=item.title,
                year=item.year or None,
                duration=float(item.duration) if item.duration else None,
                synopsis=item.description or None,
                service=self.ID,
            )
        if item.kind == "tvepisode":
            return Title(
                id=item.content_id,
                kind=TitleKind.EPISODE,
                name=item.title,
                season=item.season or None,
                episode=item.episode or None,
                episode_name=item.episode_name or None,
                duration=float(item.duration) if item.duration else None,
                synopsis=item.description or None,
                service=self.ID,
            )
        if item.kind == "liveevent":
            return Title(
                id=item.live_content_id,
                kind=TitleKind.PROGRAM,
                name=item.title,
                channel=item.competition or "TVNZ+",
                starts_at=item.starts_at,
                ends_at=item.ends_at,
                synopsis=item.description or None,
                service=self.ID,
            )
        return Title(
            id=item.content_id,
            kind=TitleKind.CLIP,
            name=item.title,
            duration=float(item.duration) if item.duration else None,
            synopsis=item.description or None,
            service=self.ID,
        )

    def _playback(
        self,
        title: Title,
        source: api.Source,
        *,
        note: str = "",
    ) -> Playback:
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={
                "User-Agent": api.USER_AGENT,
                "Accept": "application/dash+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            proxy=self.ctx.proxy,
            note=note or source.line(),
        )
        if source.protected:
            playback.drm = DrmInfo(
                system="widevine",
                license_url=source.license_url,
                headers={
                    "Authorization": f"Bearer {source.edge_access_token}",
                    "Content-Type": "application/octet-stream",
                },
                context={"edge_access_token": source.edge_access_token},
            )
        else:
            playback.drm = DrmInfo(clear=True)
        return playback

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        token = str((drm.context or {}).get("edge_access_token") or "")
        try:
            return self._licence_client().widevine_license(
                drm.license_url,
                challenge,
                edge_access_token=token,
            )
        except api.TvnzError as exc:
            raise CdmError(str(exc)) from exc

    def _licence_client(self) -> api.TvnzApi:
        if self._api is None:
            self._api = self._new_client()
        return self._api


__all__ = ["TVNZ", "api"]

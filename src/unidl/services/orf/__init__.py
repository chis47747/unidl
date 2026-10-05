"""ORF ON native service: anonymous/TV auth, VOD, live, search and Widevine."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from ...core.cdm import CdmError
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_LOGIN_MODE = Setting(
    key="login_mode",
    label="Access mode",
    kind="choice",
    options=[
        Option("anonymous", "Anonymous"),
        Option("tv", "ORF account with TV code"),
    ],
    default="anonymous",
    help="Anonymous access uses the television client credential. TV mode keeps and refreshes orf_token.json.",
    resets_session=True,
)

_SOURCE_TIER = Setting(
    key="source_tier",
    label="ORF source tier",
    kind="choice",
    options=[
        Option("hd", "HD"),
        Option("fhd", "Full HD"),
        Option("uhd", "UHD when available"),
    ],
    default="uhd",
    help="Selects the ORF API source before UniDL applies the shared track settings.",
)


@registry.register
class Orf(Service):
    """ORF ON using the 6.11.9 television API contract.

    Authorization: anonymous TV-client Basic auth, or OIDC TV code with refresh.
    Geofence:      Austria for titles whose rights are not worldwide.
    Playback:      DASH/HLS catalogue sources and raw Widevine licences.
    Lifecycle:     metadata authorization opens no platform playback session.
    """

    ID = "orf"
    NAME = "ORF ON"
    TAG = "ORF"
    ALIASES = ("orf", "orf on", "tvthek", "on.orf.at")
    TITLE_RE = r"(?:on|api-tvthek)\.orf\.at/(?:video|sendereihe|profile|api/)"
    GEOFENCE = ("AT",)
    DESCRIPTION = "ORF ON television API with VOD, programme profiles, live TV and search."

    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    SUPPORTS_LIBRARY = False
    SETTINGS = [_LOGIN_MODE, _SOURCE_TIER]

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._api: api.OrfApi | None = None
        self._api_mode = ""
        self._adult_access_granted = False
        self._auth_error = ""

    # ---------------------------------------------------------------- auth
    def login_mode(self) -> str:
        return api.normalize_login_mode(self.settings.get("login_mode", "anonymous"))

    def source_tier(self) -> str:
        return api.normalize_source_tier(self.settings.get("source_tier", "uhd"))

    def _cached_state(self) -> api.Session:
        return api.Session.from_cache(self.ctx.tokens.read(api.TOKEN_FILE))

    def auth_status(self) -> AuthStatus:
        mode = self.login_mode()
        if mode == "anonymous":
            return AuthStatus(False, "anonymous television access", anonymous_ok=True)
        if self._auth_error:
            return AuthStatus(False, self._auth_error, detail="ORF token retained only if refreshable")
        try:
            state = self._cached_state()
        except api.OrfError as exc:
            return AuthStatus(False, str(exc), detail=f"Remove {api.TOKEN_FILE} or sign in again")
        if state.token_current():
            return AuthStatus(True, state.label(), detail="ORF TV token")
        if state.refresh_token:
            return AuthStatus(True, state.label(), detail="ORF TV token · refresh on use")
        return AuthStatus(False, "ORF TV-code sign-in required")

    def _save_state(self, state: api.Session) -> None:
        if not state.access_token and not state.refresh_token:
            self.ctx.tokens.remove(api.TOKEN_FILE)
            return
        written = self.ctx.tokens.write(api.TOKEN_FILE, state.to_cache())
        try:
            Path(written).chmod(0o600)
        except OSError as exc:
            raise api.OrfError(f"Could not secure {api.TOKEN_FILE}") from exc

    def _new_client(self) -> api.OrfApi:
        mode = self.login_mode()
        state = self._cached_state() if mode == "tv" else api.Session()
        return api.OrfApi(
            self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            login_mode=mode,
            state=state,
            on_save=self._save_state,
        )

    def client(self) -> api.OrfApi:
        mode = self.login_mode()
        if self._api is None or self._api_mode != mode:
            self._api = self._new_client()
            self._api_mode = mode
            self._adult_access_granted = False
        return self._api

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        mode = self.login_mode()
        if mode == "anonymous":
            self._api = self._new_client()
            self._api_mode = mode
            self._auth_error = ""
            ctx.log("ORF anonymous television access is ready", "ok")
            return

        client = self._new_client()
        try:
            ctx.status("Requesting an ORF TV code")
            challenge = client.begin_tv_login()
            lines: list[str | tuple[str, str]] = [
                ("Code", challenge.user_code),
                ("Open", challenge.direct_url or challenge.verification_url),
            ]
            if challenge.direct_url and challenge.direct_url != challenge.verification_url:
                lines.append(("TV page", challenge.verification_url))
            lines.append("Complete the ORF sign-in on that page. Approval is detected automatically.")
            confirmed = yield ctx.wait_for(
                "Sign in to ORF ON",
                lines,
                poll=lambda: client.poll_tv_login(challenge),
                timeout=challenge.expires_in,
                interval=challenge.interval,
            )
        except Back:
            return
        except api.OrfError as exc:
            ctx.error(str(exc))
            return
        if not isinstance(confirmed, api.Session) or not confirmed.access_token:
            ctx.error("ORF TV activation completed without a usable token")
            return

        self._api = client
        self._api_mode = mode
        self._auth_error = ""
        self._adult_access_granted = False
        try:
            client.account_info()
        except api.OrfError as exc:
            ctx.warn(f"ORF TV sign-in succeeded, but account details were unavailable: {exc}")
        ctx.log(f"ORF TV sign-in completed · {client.state.label()}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(api.TOKEN_FILE)
        self._api = None
        self._api_mode = ""
        self._adult_access_granted = False
        self._auth_error = ""
        super().logout()

    def _authenticated(self, ctx: FlowContext) -> Iterator[Ask]:
        yield from ()
        try:
            client = self.client()
        except api.OrfError as exc:
            ctx.error(str(exc))
            return None
        if client.login_mode == "anonymous":
            return client
        try:
            had_current_token = client.state.token_current()
            client.ensure_session()
            if not had_current_token:
                ctx.log("ORF TV token refreshed", "ok")
            self._auth_error = ""
            return client
        except api.AuthenticationRequired:
            ctx.warn("ORF TV sign-in is required")
        except api.OrfError as exc:
            self._auth_error = str(exc)
            ctx.error(str(exc))
            return None

        yield from self.begin_login(ctx)
        client = self._api
        if client is None or client.login_mode != "tv" or not client.state.token_current():
            return None
        return client

    def _adult_access(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        video: api.Video,
    ) -> Iterator[Ask]:
        yield from ()
        if self._adult_access_granted or not video.active_youth_protection:
            return True
        if client.login_mode != "tv":
            ctx.error("This ORF title currently requires TV sign-in and adult access")
            return False
        state = client.pin_state()
        if not (state.exists and state.enabled):
            ctx.log("ORF account does not require an active adult PIN check", "info")
            return True
        while True:
            pin = str((yield ctx.text("ORF adult PIN", password=True)) or "").strip()
            if not pin:
                ctx.warn("The ORF adult PIN cannot be empty")
                continue
            if client.verify_pin(pin):
                self._adult_access_granted = True
                ctx.log("ORF adult PIN accepted", "ok")
                return True
            ctx.warn("That ORF adult PIN was not accepted")

    # ------------------------------------------------------------- catalogue
    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        reference = api.parse_reference(target)
        if reference is None:
            ctx.error("Enter a complete ORF ON video, sendereihe, profile or livestream URL")
            return
        client = yield from self._authenticated(ctx)
        if not isinstance(client, api.OrfApi):
            return
        try:
            ctx.status(f"Resolving ORF content {reference.item_id}")
            yield from self._reference(ctx, client, reference)
        except api.OrfError as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            return
        client = yield from self._authenticated(ctx)
        if not isinstance(client, api.OrfApi):
            return
        try:
            ctx.status(f"Searching ORF ON for {wanted}")
            hits = client.search(wanted)
        except api.OrfError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"ORF ON found no results for {wanted!r}")
            return
        while True:
            try:
                hit = yield ctx.pick(
                    f"ORF ON · {len(hits)} result(s)",
                    [Choice(item.title, item, detail=item.detail) for item in hits],
                )
            except Back:
                return
            if not isinstance(hit, api.SearchHit):
                return
            try:
                yield from self._search_hit(ctx, client, hit)
            except Back:
                continue
            except api.OrfError as exc:
                ctx.error(str(exc))

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = yield from self._authenticated(ctx)
        if not isinstance(client, api.OrfApi):
            return
        try:
            ctx.status("Loading ORF ON live channels")
            channels = client.live_channels()
        except api.OrfError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("ORF ON returned no live channels")
            return
        while True:
            try:
                channel = yield ctx.table(
                    f"ORF ON · {len(channels)} live channels",
                    ["Channel", "Now"],
                    [(item.name, item.current) for item in channels],
                    channels,
                )
            except Back:
                return
            if not isinstance(channel, api.Channel):
                return
            try:
                video = client.live_video(channel)
                title = Title(
                    id=str(video.id),
                    kind=TitleKind.CHANNEL,
                    name=channel.name,
                    channel=channel.name,
                    episode_name=channel.current or None,
                    duration=video.duration,
                    service=self.ID,
                )
                yield from self._emit(ctx, client, video, title, live=True)
            except Back:
                continue
            except api.OrfError as exc:
                ctx.error(str(exc))

    def _reference(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        reference: api.ParsedReference,
    ) -> Iterator[Ask]:
        if reference.kind == "profile":
            yield from self._profile(ctx, client, client.profile(reference.item_id))
            return
        if reference.kind == "episode":
            episode = client.video("episode", reference.item_id)
            if reference.segment_id is not None:
                segment = next(
                    (item for item in episode.segments if item.id == reference.segment_id),
                    None,
                ) or client.video("segment", reference.segment_id)
                if segment.episode_id != episode.id:
                    raise api.OrfError(
                        f"ORF segment {segment.id} does not belong to episode {episode.id}"
                    )
                allowed = yield from self._adult_access(ctx, client, episode)
                if allowed:
                    yield from self._emit_segment(ctx, client, episode, segment)
                return
            yield from self._episode(ctx, client, episode)
            return
        if reference.kind == "segment":
            segment = client.video("segment", reference.item_id)
            episode = client.segment_episode(segment)
            allowed = yield from self._adult_access(ctx, client, episode)
            if allowed:
                yield from self._emit_segment(ctx, client, episode, segment)
            return
        if reference.kind == "livestream":
            video = client.video("livestream", reference.item_id)
            title = Title(
                id=str(video.id),
                kind=TitleKind.CHANNEL,
                name=video.title,
                channel=video.title,
                duration=video.duration,
                service=self.ID,
            )
            yield from self._emit(ctx, client, video, title, live=True)
            return
        raise api.OrfError(f"Unsupported ORF reference type: {reference.kind}")

    def _search_hit(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        hit: api.SearchHit,
    ) -> Iterator[Ask]:
        video = client.video(hit.kind, hit.id)
        if hit.kind == "episode":
            yield from self._episode(ctx, client, video)
            return
        if hit.kind == "segment":
            episode = client.segment_episode(video)
            allowed = yield from self._adult_access(ctx, client, episode)
            if allowed:
                yield from self._emit_segment(ctx, client, episode, video)
            return
        title = Title(
            id=str(video.id),
            kind=TitleKind.CHANNEL,
            name=video.title,
            channel=video.title,
            duration=video.duration,
            service=self.ID,
        )
        yield from self._emit(ctx, client, video, title, live=True)

    def _episode(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        episode: api.Video,
    ) -> Iterator[Ask]:
        allowed = yield from self._adult_access(ctx, client, episode)
        if not allowed:
            return
        if episode.content_kind == "film":
            yield from self._emit_movie(ctx, client, episode)
            return
        if episode.content_kind == "series":
            yield from self._series_profile(ctx, client, client.episode_profile(episode))
            return
        yield from self._program(ctx, client, episode)

    def _profile(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        profile: api.Profile,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading {profile.title}")
        episodes = client.profile_episodes(profile)
        if not episodes:
            ctx.warn(f"{profile.title} has no available episodes")
            return
        if profile.season_info or all(item.content_kind == "series" for item in episodes):
            yield from self._series_profile(ctx, client, profile, current_episodes=episodes)
            return
        yield from self._episode_picker(
            ctx,
            client,
            profile.title,
            episodes,
            preserve_api_order=True,
        )

    def _series_profile(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        profile: api.Profile,
        current_episodes: list[api.Video] | None = None,
    ) -> Iterator[Ask]:
        season_info = profile.season_info
        if season_info is None:
            episodes = current_episodes if current_episodes is not None else client.profile_episodes(profile)
            if not episodes:
                ctx.warn(f"{profile.title} has no available episodes")
                return
            yield from self._episode_picker(
                ctx,
                client,
                profile.title,
                episodes,
                series_title=profile.title,
                preserve_api_order=False,
            )
            return

        base_title = season_info[0]
        profiles = [profile]
        for related in client.related_profiles(profile):
            info = related.season_info
            if info and info[0].casefold() == base_title.casefold():
                profiles.append(related)

        seasons: list[tuple[int, str, list[api.Video]]] = []
        seen: set[int] = set()
        for season_profile in profiles:
            info = season_profile.season_info
            if info is None or season_profile.id in seen:
                continue
            seen.add(season_profile.id)
            episodes = (
                current_episodes
                if season_profile.id == profile.id and current_episodes is not None
                else client.profile_episodes(season_profile)
            )
            if episodes:
                seasons.append((info[1], info[2], episodes))
        seasons.sort(key=lambda item: item[0])
        if not seasons:
            raise api.OrfError(f"ORF returned no seasons for {base_title}")

        while True:
            try:
                selected = yield ctx.pick(
                    f"{base_title} · seasons",
                    [
                        Choice(label, number, detail=f"{len(episodes)} episode(s)")
                        for number, label, episodes in seasons
                    ],
                )
            except Back:
                return
            season = next((item for item in seasons if item[0] == selected), None)
            if season is None:
                return
            try:
                yield from self._episode_picker(
                    ctx,
                    client,
                    f"{base_title} {season[1]}",
                    season[2],
                    series_title=base_title,
                    season_number=season[0],
                    preserve_api_order=False,
                )
            except Back:
                continue

    def _episode_picker(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        heading: str,
        episodes: list[api.Video],
        *,
        series_title: str = "",
        season_number: int | None = None,
        preserve_api_order: bool,
    ) -> Iterator[Ask]:
        ordered = list(episodes) if preserve_api_order else sorted(episodes, key=lambda item: (item.date, item.id))
        while True:
            picked = yield ctx.pick(
                f"{heading} · {len(ordered)} episode(s)",
                [
                    Choice(
                        item.title,
                        item,
                        detail=(
                            f"{round(item.duration / 60)} min"
                            if item.duration
                            else "unavailable"
                            if not item.available
                            else f"ID {item.id}"
                        ),
                        disabled=not item.available,
                    )
                    for item in ordered
                ],
                multi=True,
                hint="space to tick, enter to confirm",
            )
            chosen = [item for item in list(picked or []) if isinstance(item, api.Video)]
            if not chosen:
                return
            if len(chosen) > 1:
                ctx.batch(len(chosen))
            return_to_list = False
            for compact in chosen:
                try:
                    episode = client.video("episode", compact.id)
                    yield from self._selected_episode(
                        ctx,
                        client,
                        episode,
                        series_title=series_title,
                        season_number=season_number,
                    )
                except Back:
                    return_to_list = True
                    break
                except api.OrfError as exc:
                    ctx.error(str(exc))
            if return_to_list:
                continue

    def _selected_episode(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        episode: api.Video,
        *,
        series_title: str = "",
        season_number: int | None = None,
    ) -> Iterator[Ask]:
        allowed = yield from self._adult_access(ctx, client, episode)
        if not allowed:
            return
        if episode.content_kind == "film":
            yield from self._emit_movie(ctx, client, episode)
            return
        yield from self._program(
            ctx,
            client,
            episode,
            series_title=series_title,
            season_number=season_number,
        )

    def _program(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        episode: api.Video,
        *,
        series_title: str = "",
        season_number: int | None = None,
    ) -> Iterator[Ask]:
        if not episode.segments or api.equivalent_full_segment(episode):
            title = self._episode_title(episode, series_title, season_number)
            yield from self._emit(ctx, client, episode, title)
            return

        while True:
            try:
                action = yield ctx.pick(
                    episode.title,
                    [
                        Choice("Full episode", "full", detail=self._duration_detail(episode)),
                        Choice(
                            f"Choose from {len(episode.segments)} segments",
                            "segments",
                            navigates=True,
                        ),
                    ],
                )
            except Back:
                return
            if action == "full":
                try:
                    title = self._episode_title(episode, series_title, season_number)
                    yield from self._emit(ctx, client, episode, title)
                except Back:
                    continue
                continue
            if action != "segments":
                return
            try:
                picked = yield ctx.pick(
                    f"{episode.title} · segments",
                    [
                        Choice(
                            segment.title,
                            segment,
                            detail=self._duration_detail(segment),
                            disabled=not segment.available,
                        )
                        for segment in episode.segments
                    ],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                continue
            chosen = [item for item in list(picked or []) if isinstance(item, api.Video)]
            if not chosen:
                continue
            if len(chosen) > 1:
                ctx.batch(len(chosen))
            for segment in chosen:
                try:
                    yield from self._emit_segment(ctx, client, episode, segment)
                except Back:
                    break

    @staticmethod
    def _duration_detail(video: api.Video) -> str:
        if not video.duration:
            return f"ID {video.id}"
        minutes, seconds = divmod(int(video.duration), 60)
        return f"{minutes}:{seconds:02d} · ID {video.id}"

    def _episode_title(
        self,
        episode: api.Video,
        series_title: str,
        season_number: int | None,
    ) -> Title:
        if series_title:
            return Title(
                id=str(episode.id),
                kind=TitleKind.EPISODE,
                name=series_title,
                season=season_number,
                episode=episode.episode_number,
                episode_name=episode.episode_name or None,
                duration=episode.duration,
                service=self.ID,
            )
        return Title(
            id=str(episode.id),
            kind=TitleKind.EPISODE,
            name=episode.title,
            duration=episode.duration,
            service=self.ID,
        )

    def _emit_movie(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        movie: api.Video,
    ) -> Iterator[Ask]:
        title = Title(
            id=str(movie.id),
            kind=TitleKind.MOVIE,
            name=movie.title,
            year=movie.production_year or None,
            duration=movie.duration,
            service=self.ID,
            data={"production_country": movie.production_country},
        )
        yield from self._emit(ctx, client, movie, title)

    def _emit_segment(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        episode: api.Video,
        segment: api.Video,
    ) -> Iterator[Ask]:
        title = Title(
            id=str(segment.id),
            kind=TitleKind.CLIP,
            name=episode.title,
            episode_name=segment.title if segment.title != episode.title else None,
            duration=segment.duration,
            service=self.ID,
        )
        yield from self._emit(ctx, client, segment, title)

    # -------------------------------------------------------------- playback
    def _emit(
        self,
        ctx: FlowContext,
        client: api.OrfApi,
        video: api.Video,
        title: Title,
        *,
        live: bool = False,
    ) -> Iterator[Ask]:
        ctx.status(f"Authorizing {title.full_label()}")
        source = client.source(video, source_tier=self.source_tier(), live=live)
        drm = None
        if source.encrypted:
            drm = DrmInfo(
                system="widevine",
                license_url=source.license_endpoint,
                context={
                    "brand_guid": source.brand_guid,
                    "user_token": source.user_token,
                },
            )
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest_url,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=live,
            drm=drm,
            extra_args=["--append-url-params"] if urlsplit(source.manifest_url).query else [],
            note=source.line,
        )
        yield ctx.emit(playback)

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        brand_guid = str(drm.context.get("brand_guid") or "")
        user_token = str(drm.context.get("user_token") or "")
        try:
            return self.client().license(
                challenge,
                drm.license_url,
                brand_guid,
                user_token,
            )
        except api.OrfError as exc:
            raise CdmError(str(exc)) from exc


__all__ = ["Orf", "api"]

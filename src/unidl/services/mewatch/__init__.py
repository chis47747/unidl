"""mewatch - TV-code sign-in, catalogue, search, live and Axis playback."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

from ...core.cdm import CdmError
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, ExternalTrack, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


@registry.register
class Mewatch(Service):
    ID = "mewatch"
    NAME = "mewatch"
    TAG = "MEWATCH"
    ALIASES = ("me watch", "mediacorp")
    TITLE_RE = r"(?:www\.)?mewatch\.sg/"
    GEOFENCE = ("SG",)
    DESCRIPTION = (
        "Mediacorp mewatch: Auth0 TV-code sign-in, homepage catalogue, search, "
        "live channels and Axis DASH/HLS playback."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.MewatchApi | None = None

    # -------------------------------------------------------------------- auth
    def auth_status(self) -> AuthStatus:
        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if state.signed_in:
            profile = state.profile_name or "TV profile"
            account = state.token("UserAccount", fresh=False)
            profile_token = state.token("UserProfile", fresh=False)
            fresh = state.auth0_fresh() and bool(account and account.fresh()) and bool(
                profile_token and profile_token.fresh()
            )
            label = f"signed in · {profile}" if fresh else f"ready to refresh · {profile}"
            return AuthStatus(True, label, detail="mewatch_token.json", anonymous_ok=True)
        anonymous = state.token("Anonymous", fresh=False)
        return AuthStatus(
            logged_in=False,
            label="anonymous",
            detail="saved TV identity" if anonymous else "new TV identity on first use",
            anonymous_ok=True,
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        if client.state.signed_in:
            try:
                ctx.status("Refreshing the saved mewatch TV session")
                client.refresh()
            except api.MewatchError:
                client.state.clear_account()
                self._save_state(client.state)
                ctx.warn("The saved mewatch session expired; requesting a new TV code")
            else:
                self._api = client
                ctx.log(f"mewatch session ready · {client.state.profile_name or 'TV profile'}", "ok")
                return

        try:
            ctx.status("Requesting a mewatch TV code")
            challenge = client.start_tv_code()
        except api.MewatchError as exc:
            ctx.error(str(exc))
            return

        def poll() -> api.Session | None:
            return client.poll_tv_code(challenge)

        try:
            state = yield ctx.wait_for(
                "Activate mewatch on another device",
                [
                    ("Code", challenge.user_code),
                    ("Open", challenge.verification_uri_complete or challenge.verification_uri),
                ],
                poll=poll,
                timeout=float(max(60, min(challenge.expires_in, 900))),
                interval=float(challenge.interval),
                hint="Open the activation page, enter the code and complete sign-in.",
            )
        except api.MewatchError as exc:
            ctx.error(str(exc))
            return
        if not isinstance(state, api.Session) or not state.signed_in:
            ctx.error("mewatch activation completed without a usable account session")
            return

        try:
            yield from self._select_login_profile(ctx, client)
        except api.MewatchError as exc:
            ctx.error(str(exc))
            return
        self._save_state(client.state)
        self._api = client
        ctx.log(f"mewatch activated · {client.state.profile_name or 'TV profile'}", "ok")

    def _select_login_profile(self, ctx: FlowContext, client: api.MewatchApi) -> Iterator[Ask]:
        profiles = client.profiles()
        if len(profiles) <= 1:
            return
        current_id = str(client.state.profile.get("id") or "")
        current_index = next(
            (index for index, profile in enumerate(profiles) if str(profile.get("id") or "") == current_id),
            0,
        )
        selected = yield ctx.pick(
            "Choose a mewatch profile",
            [
                Choice(
                    str(profile.get("name") or "Profile"),
                    profile,
                    detail="PIN required" if profile.get("isRestricted") else "",
                )
                for profile in profiles
            ],
            cursor=current_index,
        )
        if not isinstance(selected, dict) or not selected.get("id"):
            return
        if str(selected.get("id")) == current_id:
            return
        pin = ""
        if selected.get("isRestricted"):
            answer = yield ctx.text(
                f"PIN for {selected.get('name') or 'profile'}",
                placeholder="Profile PIN",
                password=True,
            )
            pin = str(answer or "")
            if not pin:
                raise api.MewatchError("A PIN is required for that mewatch profile")
        ctx.status(f"Opening profile · {selected.get('name') or 'mewatch'}")
        client.select_profile(str(selected["id"]), pin)

    def logout(self) -> None:
        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        state.clear_account()
        self._save_state(state)
        self._api = None
        super().logout()

    def _new_client(self, state: api.Session | None = None) -> api.MewatchApi:
        return api.MewatchApi(
            session=self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            state=state or api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE)),
            on_save=self._save_state,
        )

    def _save_state(self, state: api.Session) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def client(self, ctx: FlowContext | None = None) -> api.MewatchApi:
        client = self._api or self._new_client()
        if ctx is not None:
            ctx.status("Opening mewatch session")
        try:
            client.ensure_auth()
        except api.MewatchError:
            if client.state.signed_in:
                raise
            client.ensure_anonymous()
        self._save_state(client.state)
        self._api = client
        return client

    # ---------------------------------------------------------------- browsing
    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                "That does not look like a mewatch URL or content ID.\n"
                "Expected https://www.mewatch.sg/watch/...-12345, a numeric ID, or home/movies/series."
            )
            return
        try:
            client = self.client(ctx)
            if parsed.kind == "page":
                yield from self._page(ctx, client, parsed.value)
                return
            ctx.status(f"Loading mewatch item {parsed.value}")
            item = client.item(parsed.value)
        except api.MewatchError as exc:
            ctx.error(str(exc))
            return
        yield from self._open_item(ctx, client, item)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            return
        try:
            client = self.client(ctx)
            ctx.status(f"Searching mewatch for {wanted}")
            results = client.search(wanted)
        except api.MewatchError as exc:
            ctx.error(str(exc))
            return
        if not results:
            ctx.warn(f"mewatch found nothing for {wanted!r}")
            return

        while True:
            try:
                selected = yield ctx.pick(
                    f"mewatch  ·  {wanted}  ·  {len(results)} results",
                    [
                        Choice(item.label, item, detail=item.detail, tags=(item.type_label,))
                        for item in results
                    ],
                )
            except Back:
                return
            if selected is None:
                return
            try:
                yield from self._open_item(ctx, client, selected)
            except Back:
                continue

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading mewatch live channels")
            channels = client.live_channels()
        except api.MewatchError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("mewatch returned no live channels")
            return
        selected = yield ctx.pick(
            f"mewatch Live  ·  {len(channels)} channels",
            [Choice(channel.name, channel, detail=channel.detail) for channel in channels],
        )
        if selected is not None:
            yield from self._emit_live(ctx, client, selected)

    def _page(self, ctx: FlowContext, client: api.MewatchApi, path: str) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading mewatch {path}")
            rails = client.page(path)
        except api.MewatchError as exc:
            ctx.error(str(exc))
            return
        if not rails:
            ctx.warn(f"mewatch returned no prefetched catalogue rows for {path}")
            return
        while True:
            try:
                rail = yield ctx.pick(
                    f"mewatch  ·  {path}",
                    [
                        Choice(
                            entry.title,
                            entry,
                            detail=f"{entry.size or len(entry.items)} title(s)",
                        )
                        for entry in rails
                    ],
                )
            except Back:
                return
            if rail is None:
                return
            try:
                if not rail.items or (rail.size > 0 and len(rail.items) < rail.size):
                    ctx.status(f"Loading {rail.title}")
                rail_items = client.rail_items(rail)
            except api.MewatchError as exc:
                ctx.error(str(exc))
                continue
            if not rail_items:
                ctx.warn(f"mewatch returned no titles for {rail.title}")
                continue
            while True:
                try:
                    selected = yield ctx.pick(
                        f"mewatch  ·  {rail.title}",
                        [
                            Choice(item.label, item, detail=item.detail, tags=(item.type_label,))
                            for item in rail_items
                        ],
                    )
                except Back:
                    break
                if selected is None:
                    return
                try:
                    yield from self._open_item(ctx, client, selected)
                except Back:
                    continue

    def _open_item(self, ctx: FlowContext, client: api.MewatchApi, item: api.Item) -> Iterator[Ask]:
        if item.is_show:
            yield from self._show(ctx, client, item)
            return
        if item.type == "link" and item.path:
            parsed = api.parse_input(f"https://www.mewatch.sg{item.path}")
            if parsed is not None and parsed.kind == "item":
                try:
                    item = client.item(parsed.value)
                except api.MewatchError as exc:
                    ctx.error(str(exc))
                    return
        yield from self._emit_item(ctx, client, item)

    def _show(self, ctx: FlowContext, client: api.MewatchApi, show: api.Item) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading episodes · {show.title}")
            seasons = client.show_seasons(show.id)
        except api.MewatchError as exc:
            ctx.error(str(exc))
            return
        if not seasons:
            ctx.warn(f"mewatch returned no episodes for {show.title}")
            return

        while True:
            if len(seasons) == 1:
                season = seasons[0]
            else:
                try:
                    season = yield ctx.pick(
                        f"{show.title}  ·  seasons",
                        [
                            Choice(entry.label, entry, detail=f"{len(entry.episodes)} episode(s)")
                            for entry in seasons
                        ],
                    )
                except Back:
                    return
            if season is None:
                return
            try:
                yield from self._episodes(ctx, client, show, season)
            except Back:
                if len(seasons) == 1:
                    return
                continue
            return

    def _episodes(
        self,
        ctx: FlowContext,
        client: api.MewatchApi,
        show: api.Item,
        season: api.Season,
    ) -> Iterator[Ask]:
        while True:
            try:
                selected = yield ctx.pick(
                    f"{show.title}  ·  {season.label}",
                    [Choice(item.label, item, detail=item.detail) for item in season.episodes],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                raise
            episodes = list(selected or [])
            if not episodes:
                return
            if len(episodes) > 1:
                ctx.batch(len(episodes))
            return_to_list = False
            for episode in episodes:
                try:
                    yield from self._emit_item(ctx, client, episode, show=show)
                except Back:
                    return_to_list = True
                    break
            if not return_to_list:
                return

    # ---------------------------------------------------------------- playback
    def _emit_item(
        self,
        ctx: FlowContext,
        client: api.MewatchApi,
        item: api.Item,
        *,
        show: api.Item | None = None,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving playback · {item.label}")
            source = client.playback(item.id, encrypted=item.encrypted)
        except api.MewatchError as exc:
            ctx.error(f"{item.label}: {exc}")
            return
        title = self._title(item, show=show)
        mux_imports = self._stage_subtitles(ctx, client, source, self.save_name(title))
        ctx.log(f"{title.full_label()}: {source.line()}", "ok")
        yield ctx.emit(self._playback(title, source, mux_imports=mux_imports))

    def _emit_live(
        self,
        ctx: FlowContext,
        client: api.MewatchApi,
        channel: api.LiveChannel,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving live playback · {channel.name}")
            channel_item = client.item(channel.id)
            source = client.playback(
                channel.id,
                is_live=True,
                encrypted=channel_item.encrypted,
            )
        except api.MewatchError as exc:
            ctx.error(f"{channel.name}: {exc}")
            return
        title = Title(
            id=channel.id,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            synopsis=channel.description or None,
            service=self.ID,
        )
        ctx.log(f"{channel.name}: {source.line()}", "ok")
        yield ctx.emit(self._playback(title, source))

    def _title(self, item: api.Item, *, show: api.Item | None = None) -> Title:
        if item.is_movie:
            return Title(
                id=item.id,
                kind=TitleKind.MOVIE,
                name=item.movie_title,
                year=str(item.year) if item.year else None,
                duration=float(item.duration) if item.duration else None,
                synopsis=item.description or None,
                service=self.ID,
            )
        series_name = (
            show.title
            if show is not None
            else str(item.raw.get("contextualTitle") or item.raw.get("showTitle") or item.title)
        )
        kind = TitleKind.EXTRA if item.type in {"clip", "extra"} else TitleKind.EPISODE
        return Title(
            id=item.id,
            kind=kind,
            name=series_name,
            year=str(item.year) if item.year else None,
            season=item.season_number or None,
            episode=item.episode_number or None,
            episode_name=item.episode_name or item.title,
            duration=float(item.duration) if item.duration else None,
            synopsis=item.description or None,
            service=self.ID,
        )

    def _stage_subtitles(
        self,
        ctx: FlowContext,
        client: api.MewatchApi,
        source: api.Source,
        save_name: str,
    ) -> list[ExternalTrack]:
        if source.is_live or not source.subtitles:
            return []
        imports: list[ExternalTrack] = []
        used: set[str] = set()
        for index, (language, url) in enumerate(source.subtitles, 1):
            tag = re.sub(r"[^A-Za-z0-9_.-]+", "-", language).strip("-.") or "und"
            if tag in used:
                tag = f"{tag}-{index:02d}"
            used.add(tag)
            try:
                content = client.subtitle_bytes(url)
                path = self._write_subtitle(save_name, tag, url, content)
            except (api.MewatchError, OSError) as exc:
                ctx.warn(f"subtitle {language or 'und'} skipped: {exc}")
                continue
            ctx.log(f"subtitle staged · {language or 'und'}", "ok")
            imports.append(
                ExternalTrack(
                    path=str(path),
                    language=language or "und",
                    name=language or "mewatch subtitle",
                )
            )
        return imports

    def _write_subtitle(
        self,
        save_name: str,
        language: str,
        url: str,
        content: bytes,
    ) -> Path:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in {".vtt", ".srt", ".ttml", ".dfxp"}:
            suffix = ".vtt"
        stem = re.sub(r"[^\w.-]+", ".", save_name, flags=re.UNICODE)
        stem = re.sub(r"\.{2,}", ".", stem).strip(".")[:180] or "mewatch"
        digest = hashlib.sha256(url.encode("utf-8") + b"\0" + content).hexdigest()[:12]
        path = self.ctx.subtitle_dir() / f"{stem}.{language}.{digest}{suffix}"
        if not path.exists() or path.read_bytes() != content:
            path.write_bytes(content)
            os.chmod(path, 0o600)
        return path

    def _playback(
        self,
        title: Title,
        source: api.Source,
        *,
        mux_imports: list[ExternalTrack] | None = None,
    ) -> Playback:
        media_headers = {"User-Agent": api.USER_AGENT, **dict(source.headers)}
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers=media_headers,
            proxy=self.ctx.proxy,
            is_live=source.is_live,
            note=source.line(),
            mux_imports=list(mux_imports or []),
        )
        if source.encrypted:
            playback.drm = DrmInfo(
                system="widevine",
                license_url=source.license_url,
                headers={
                    "User-Agent": api.USER_AGENT,
                    "Content-Type": "application/octet-stream",
                    **dict(source.headers),
                },
            )
        else:
            playback.drm = DrmInfo(clear=True)
        return playback

    # ------------------------------------------------------------------- drm
    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        if not drm.license_url:
            raise CdmError("mewatch playback returned no Widevine licence URL")
        try:
            client = self._api or self._new_client()
            return client.widevine_license(drm.license_url, challenge, headers=drm.headers)
        except api.MewatchError as exc:
            raise CdmError(str(exc)) from exc


__all__ = ["Mewatch", "api"]

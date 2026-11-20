"""ARD Mediathek - German public broadcaster VOD, search and live TV."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import ExternalTrack, Playback
from ...core.secureio import atomic_write_bytes, private_directory
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from ...downloader.subtitles import SubtitleConversionError, convert_subtitle_file
from . import api

_LOGIN_MODE = Setting(
    key="login_mode",
    label="Login mode",
    kind="choice",
    options=[
        Option("anonymous", "Anonymous - free catalogue"),
        Option("tv", "TV device code - unlocks PIN content"),
    ],
    default="tv",
    help="Most titles play anonymously. Choose TV login for FSK/adult PIN titles.",
    resets_session=False,
)

_STREAM_EDITION = Setting(
    key="stream_edition",
    label="Stream edition",
    kind="choice",
    options=[
        Option("normal", "Normal"),
        Option("audio_description", "Audiodeskription"),
    ],
    default="normal",
    help=(
        "Selects the ARD mediaCollection stream before its manifest is passed "
        "to UniDL; this is separate from unidl.downloader track selection."
    ),
    resets_session=False,
)


def _is_probe_miss(error: api.ArdError) -> bool:
    """Whether an unknown reference can safely try its next ARD shape."""
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "http 404",
            "returned no item",
            "returned no series",
            "no episodes found",
            "not an item",
            "not a grouping",
        )
    )


@registry.register
class Ard(Service):
    ID = "ard"
    NAME = "ARD Mediathek"
    TAG = "ARD"
    ALIASES = ("ardmediathek", "ard-mediathek", "daserste")
    TITLE_RE = r"^https://(?:(?:www|api)\.)?ardmediathek\.de/"
    GEOFENCE = ("DE",)
    DESCRIPTION = (
        "German public-service VOD, search and live channels. Clear HLS; "
        "optional TV login for PIN-protected titles."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ()
    SETTINGS = [_LOGIN_MODE, _STREAM_EDITION]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    TOKEN_FILE = api.TOKEN_FILE
    PREVIOUS_TOKEN_FILE = api.PREVIOUS_TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.ArdApi | None = None

    def auth_status(self) -> AuthStatus:
        mode = self._login_mode()
        raw = self._load_auth() or {}
        state = api.auth_from_cache(raw)
        if state and state.signed_in:
            fresh = bool(state.id_token and api.token_is_fresh(state.id_token))
            label = f"TV · {state.display_name}"
            if not fresh and state.refresh_token:
                label += " · refreshable"
            return AuthStatus(
                logged_in=True,
                anonymous_ok=True,
                label=label,
                detail="token cache",
            )
        if mode == "tv":
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                label="anonymous (TV login optional)",
                detail="PIN content needs Sign in",
            )
        return AuthStatus(
            logged_in=False,
            anonymous_ok=True,
            label="anonymous",
            detail="free catalogue",
        )

    def _login_mode(self) -> str:
        value = str(self.settings.get("login_mode") or "tv").strip().lower()
        if value in {"tv", "device", "code", "tv_code", "tv-code"}:
            return "tv"
        return "anonymous"

    def _audio_description_mode(self) -> bool:
        value = str(self.settings.get("stream_edition") or "normal").strip().casefold()
        return value in {
            "audio_description",
            "audio-description",
            "audiodeskription",
            "ad",
        }

    def _load_auth(self) -> dict[str, Any] | None:
        if self.ctx.tokens.resolve(self.TOKEN_FILE) is not None:
            return self.ctx.tokens.read(self.TOKEN_FILE)
        previous = self.ctx.tokens.resolve(self.PREVIOUS_TOKEN_FILE)
        if previous is None:
            return None
        adopted = self.ctx.tokens.import_legacy(self.TOKEN_FILE, previous)
        if adopted is not None:
            self.ctx.tokens.remove(self.PREVIOUS_TOKEN_FILE)
        return adopted

    def _save_auth(self, state: api.AuthState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.as_cache())
        self.ctx.tokens.remove(self.PREVIOUS_TOKEN_FILE)

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        cached = api.auth_from_cache(self._load_auth())
        if cached and cached.signed_in:
            client.set_auth(cached)
            if client.ensure_token():
                try:
                    client.fetch_profiles()
                except api.ArdError as exc:
                    ctx.error(f"Could not load ARD TV profiles: {exc}")
                    return
                self._save_auth(client.auth)
                self._api = client
                ctx.log(f"ARD TV session ready · {client.auth.display_name}", "ok")
                return
        try:
            ctx.status("Creating an ARD TV login code")
            device = client.start_device_code()
        except api.ArdError as exc:
            ctx.error(str(exc))
            return

        lines: list[str | tuple[str, str]] = [("Code", str(device.user_code))]
        if device.verification_uri:
            lines.append(("Open", str(device.verification_uri)))
        if device.qr_code_uri:
            lines.append(("QR image", str(device.qr_code_uri)))

        def poll() -> api.AuthState | api.DevicePollFailure | None:
            try:
                return client.poll_device_code(device)
            except api.ArdError as exc:
                return api.DevicePollFailure(str(exc))

        try:
            state = yield ctx.wait_for(
                "Confirm ARD TV sign-in",
                lines,
                poll,
                timeout=float(max(5, min(device.expires_in or 180, 600))),
                interval=float(device.interval or 5),
                hint="Open the page, enter the code, then return here.",
            )
        except api.ArdError as exc:
            ctx.error(str(exc))
            return
        if isinstance(state, api.DevicePollFailure):
            ctx.error(state.message)
            return
        if not isinstance(state, api.AuthState) or not state.signed_in:
            ctx.error("ARD TV login finished without a usable session")
            return
        self._api = client
        ctx.log(f"signed in as {state.display_name}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, {})
        self.ctx.tokens.remove(self.PREVIOUS_TOKEN_FILE)
        self._api = None
        super().logout()

    def _new_client(self) -> api.ArdApi:
        return api.ArdApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            on_save=self._save_auth,
        )

    def client(self, ctx: FlowContext | None = None) -> api.ArdApi:
        if self._api is not None:
            return self._api
        client = self._new_client()
        state = api.auth_from_cache(self._load_auth())
        if state and state.signed_in and self._login_mode() == "tv":
            client.set_auth(state)
            if ctx:
                ctx.status("Refreshing ARD TV session")
            if not client.ensure_token():
                if ctx:
                    ctx.warn("ARD TV session expired; continuing anonymously")
                client.set_auth(None)
            else:
                self._save_auth(client.auth)
        self._api = client
        return client

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like an ARD Mediathek link or id: {target}\n"
                "Expected /video/, /film/, /serie/, /live/, a page-gateway href, "
                "or a content id."
            )
            return
        client = self.client(ctx)
        yield from self._route(ctx, client, parsed)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        client = self.client(ctx)
        wanted = str(query or "").strip()
        if not wanted:
            return
        page_number = 0
        hits: list[api.SearchHit] = []
        seen: set[tuple[Any, ...]] = set()
        warnings_seen: set[str] = set()
        load_page = True
        has_next = False
        while True:
            if load_page:
                try:
                    ctx.status(f"Searching ARD for {wanted}")
                    page = client.search_page(wanted, page_number)
                except api.ArdError as exc:
                    ctx.error(str(exc))
                    return
                for warning in client.search_warnings:
                    if warning not in warnings_seen:
                        warnings_seen.add(warning)
                        ctx.warn(warning)
                for hit in page.hits:
                    identity = (
                        hit.ref.kind,
                        hit.ref.partner,
                        hit.ref.id
                        or hit.ref.href
                        or (hit.title, hit.content_type, hit.core_asset_type),
                    )
                    if identity not in seen:
                        seen.add(identity)
                        hits.append(hit)
                if not hits:
                    ctx.warn(f"ARD found nothing for {wanted}")
                    return
                has_next = page.has_next
                load_page = False
            choices = [
                Choice(
                    hit.title,
                    hit,
                    detail=(hit.synopsis or hit.ref.href)[:90],
                    tags=(hit.type_label,) if hit.type_label else (),
                )
                for hit in hits
            ]
            if has_next:
                choices.append(
                    Choice(
                        "Next page",
                        ("next", page_number + 1),
                        detail="Load more ARD search results",
                        navigates=True,
                    )
                )
            try:
                chosen = yield ctx.pick(
                    f"ARD  ·  {wanted}  ·  {len(hits)} results",
                    choices,
                )
            except Back:
                return
            if chosen is None:
                return
            if (
                isinstance(chosen, tuple)
                and len(chosen) == 2
                and chosen[0] == "next"
            ):
                page_number = int(chosen[1])
                load_page = True
                continue
            if not isinstance(chosen, api.SearchHit):
                return
            try:
                yield from self._route(ctx, client, chosen.ref, hit=chosen)
            except Back:
                continue
            except api.ArdError as exc:
                ctx.error(str(exc))

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        try:
            ctx.status("Loading ARD live channels")
            channels = client.channels()
        except api.ArdError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("ARD returned no live channels")
            return
        while True:
            try:
                chosen = yield ctx.table(
                    f"ARD Live  ·  {len(channels)} channels",
                    ["Channel", "Now", "Partner", "Id"],
                    [
                        (channel.name, channel.now or "-", channel.partner, channel.id[:24])
                        for channel in channels
                    ],
                    channels,
                )
            except Back:
                return
            if chosen is None:
                return
            yield from self._emit_live(ctx, client, chosen)

    def _route(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        ref: api.ContentRef,
        *,
        hit: api.SearchHit | None = None,
    ) -> Iterator[Ask]:
        kind = ref.kind
        core = ((hit.core_asset_type if hit else "") or "").upper()
        item_id = ref.id
        partner = ref.partner or "ard"

        if kind == "live" or core in api.LIVE_TYPES:
            if not item_id:
                ctx.error("Could not resolve live item id")
                return
            channel = api.Channel(
                id=item_id,
                partner=partner,
                name=hit.title if hit else item_id,
                href=ref.href,
            )
            yield from self._emit_live(ctx, client, channel)
            return

        if kind in {"editorial", "compilation"}:
            href = ref.href
            if not href and item_id:
                href = f"{api.PAGE}/pages/{partner}/{kind}/{item_id}"
            if href:
                yield from self._page(ctx, client, href, hit.title if hit else "Page")
            return

        if kind == "grouping" or core in api.SERIES_TYPES:
            if not item_id:
                ctx.error("Could not resolve series id")
                return
            if core == "SINGLE":
                try:
                    loaded = client.series(item_id, partner, single=True)
                except api.ArdError as exc:
                    ctx.error(str(exc))
                    return
                yield from self._series(ctx, client, item_id, partner, loaded=loaded)
            else:
                yield from self._series(ctx, client, item_id, partner)
            return

        if kind == "item" or core in api.VIDEO_TYPES:
            if not item_id:
                ctx.error("Could not resolve item id")
                return
            yield from self._video(
                ctx,
                client,
                item_id,
                partner,
                content_type=core,
                title_hint=hit.title if hit else "",
            )
            return

        if item_id:
            probe_error: api.ArdError | None = None
            try:
                item = client.fetch_item(item_id, partner)
                if client.player_widgets(item):
                    yield from self._video(ctx, client, item_id, partner, item=item)
                    return
            except api.ArdError as exc:
                probe_error = exc

            if probe_error is not None and not _is_probe_miss(probe_error):
                ctx.error(str(probe_error))
                return

            try:
                series = client.series(item_id, partner)
            except api.ArdError as exc:
                if not _is_probe_miss(exc):
                    ctx.error(str(exc))
                    return
                series = None
            if series is not None:
                yield from self._series(ctx, client, item_id, partner, loaded=series)
                return
        page_ref = api.parse_content_reference(ref.href) if ref.href else None
        page_kind = page_ref.kind if page_ref else ""
        if ref.href and (ref.kind in {"editorial", "compilation"} or page_kind in {"editorial", "compilation"}):
            yield from self._page(ctx, client, ref.href, hit.title if hit else "Page")
            return
        ctx.error("Could not fetch content. It may be unavailable or removed.")

    def _page(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        href: str,
        title: str,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading {title}")
            page = client.fetch_page(href)
        except api.ArdError as exc:
            ctx.error(str(exc))
            return
        players = api.ArdApi.player_widgets(page)
        if players:
            ref = api.parse_content_reference(href)
            item_id = str(players[0].get("id") or ref.id)
            if item_id:
                yield from self._video(ctx, client, item_id, ref.partner or "ard", item=page)
                return
        teasers = client.extract_teasers(page)
        if not teasers:
            ctx.warn(f"No results on page {title}")
            return
        hits: list[api.SearchHit] = []
        seen: set[tuple[Any, ...]] = set()
        for teaser in teasers:
            if not isinstance(teaser, dict):
                continue
            ref = client.teaser_reference(teaser)
            if not ref.id and not ref.href:
                continue
            if ref.id:
                key = ("ref", ref.kind, ref.partner, ref.id)
            elif ref.href:
                key = ("href", ref.href)
            else:
                key = ("title", api.teaser_title(teaser))
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                api.SearchHit(
                    title=api.teaser_title(teaser),
                    ref=ref,
                    content_type=str(teaser.get("coreAssetType") or teaser.get("type") or ""),
                    synopsis=str(teaser.get("teaserText") or ""),
                    core_asset_type=str(teaser.get("coreAssetType") or ""),
                )
            )
        if not hits:
            ctx.warn(f"No routable results on page {title}")
            return
        while True:
            try:
                chosen = yield ctx.pick(
                    f"ARD  ·  {title}",
                    [
                        Choice(
                            hit.title,
                            hit,
                            detail=hit.synopsis[:90],
                            tags=(hit.type_label,) if hit.type_label else (),
                        )
                        for hit in hits
                    ],
                )
            except Back:
                return
            if chosen is None:
                return
            yield from self._route(ctx, client, chosen.ref, hit=chosen)

    def _series(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        item_id: str,
        partner: str,
        *,
        loaded: api.Series | None = None,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading series {item_id[:24]}")
            series = loaded or client.series(item_id, partner)
        except api.ArdError as exc:
            ctx.error(str(exc))
            return
        ctx.log(f"{series.title} · {len(series.seasons)} season(s)", "ok")
        while True:
            try:
                season = (
                    series.seasons[0]
                    if len(series.seasons) == 1
                    else (
                        yield ctx.pick(
                            f"{series.title}  ·  seasons",
                            [Choice(entry.label, entry) for entry in series.seasons],
                        )
                    )
                )
            except Back:
                return
            if season is None:
                return
            backed = yield from self._season_episodes(ctx, client, series, season)
            if backed and len(series.seasons) == 1:
                return

    def _season_episodes(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        series: api.Series,
        season: api.Season,
    ) -> Iterator[Ask]:
        while True:
            choices: list[Choice] = [
                Choice(ep.label, ("episode", ep), detail=ep.synopsis[:90])
                for ep in season.episodes
            ]
            if season.audio_description:
                choices.append(
                    Choice(
                        f"Audiodeskription  ·  {len(season.audio_description)}",
                        ("audio_menu", season.audio_description),
                        navigates=True,
                    )
                )
            if not choices:
                ctx.warn(f"{series.title} season {season.number} has no episodes")
                return
            try:
                picked = yield ctx.pick(
                    f"{series.title}  ·  {season.label}",
                    choices,
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                return True
            selected = list(picked or [])
            if not selected:
                continue
            if len(selected) == 1 and selected[0][0] == "audio_menu":
                yield from self._pick_episodes(
                    ctx,
                    client,
                    series,
                    season.number,
                    selected[0][1],
                    title="Audiodeskription",
                )
                continue
            episodes: list[api.Episode] = []
            open_ad = False
            for action, payload in selected:
                if action == "episode":
                    episodes.append(payload)
                elif action == "audio_menu":
                    open_ad = True
            if open_ad and not episodes:
                yield from self._pick_episodes(
                    ctx,
                    client,
                    series,
                    season.number,
                    season.audio_description,
                    title="Audiodeskription",
                )
                continue
            if not episodes:
                continue
            if len(episodes) > 1:
                ctx.batch(len(episodes))
            for episode in episodes:
                if (yield from self._emit_episode(ctx, client, episode)):
                    break
            else:
                if open_ad:
                    yield from self._pick_episodes(
                        ctx,
                        client,
                        series,
                        season.number,
                        season.audio_description,
                        title="Audiodeskription",
                    )

    def _pick_episodes(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        series: api.Series,
        season_number: int,
        episodes: list[api.Episode],
        *,
        title: str,
    ) -> Iterator[Ask]:
        if not episodes:
            ctx.warn(f"No {title} episodes")
            return
        while True:
            try:
                picked = yield ctx.pick(
                    f"{series.title}  ·  S{season_number:02d}  ·  {title}",
                    [Choice(ep.label, ep, detail=ep.synopsis[:90]) for ep in episodes],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                return
            selected = list(picked or [])
            if not selected:
                continue
            if len(selected) > 1:
                ctx.batch(len(selected))
            for episode in selected:
                if (yield from self._emit_episode(ctx, client, episode)):
                    break

    def _resolve_source(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        item_id: str,
        partner: str,
        *,
        is_live: bool = False,
        audio_description: bool = False,
        item: dict | None = None,
    ) -> Iterator[Ask]:
        """Yield PIN asks if needed; return an :class:`api.Source` (PEP 380)."""
        try:
            return client.source(
                item_id,
                partner,
                is_live=is_live,
                audio_description=audio_description,
                item=item,
            )
        except api.PinRequired as pin_req:
            if self._login_mode() != "tv":
                raise api.ArdError(
                    "This content needs an adult PIN. Set Login mode to TV device "
                    "code and sign in."
                ) from pin_req
            if not client.auth.signed_in:
                state = api.auth_from_cache(self._load_auth())
                if state:
                    client.set_auth(state)
            if not client.ensure_token():
                raise api.ArdError(
                    "PIN content needs Sign in (TV device code) first"
                ) from pin_req
            if not client.auth.pin:
                try:
                    client.fetch_profiles()
                except api.ArdError as exc:
                    raise api.ArdError(f"Could not load ARD TV profiles: {exc}") from exc
            if not client.auth.pin:
                raise api.ArdError(
                    "No adult PIN found on the active TV profile"
                ) from pin_req
            for attempt in range(3):
                entered = yield ctx.text("Adult PIN", placeholder="PIN", password=True)
                pin = str(entered or "").strip()
                if not pin:
                    if attempt == 2:
                        raise api.ArdError("No PIN entered") from pin_req
                    ctx.warn("No PIN entered")
                    continue
                try:
                    return client.unlock_with_pin(
                        item_id,
                        partner,
                        pin,
                        is_live=is_live,
                        audio_description=audio_description,
                    )
                except api.ArdError as exc:
                    if "Incorrect" in str(exc) and attempt < 2:
                        ctx.warn(f"{exc} · {2 - attempt} attempt(s) left")
                        continue
                    raise

    def _video(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        item_id: str,
        partner: str,
        *,
        item: dict | None = None,
        is_live: bool = False,
        content_type: str = "",
        title_hint: str = "",
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading {item_id[:32]}")
            item = item or client.fetch_item(item_id, partner)
            video = client.video_from_item(
                item,
                item_id,
                partner,
                is_live=is_live,
                content_type=content_type,
                title_hint=title_hint,
            )
            audio_description = (
                video.audio_description or self._audio_description_mode()
            )
            source = yield from self._resolve_source(
                ctx,
                client,
                item_id,
                partner,
                is_live=is_live,
                audio_description=audio_description,
                item=item,
            )
        except Back:
            return
        except api.ArdError as exc:
            ctx.error(str(exc))
            return
        if is_live:
            title = Title(
                id=item_id,
                kind=TitleKind.CHANNEL,
                name=video.title,
                channel=video.title,
                duration=video.duration,
                service=self.ID,
            )
        elif video.kind == "episode":
            episode_name = video.title
            if audio_description:
                episode_name = f"{episode_name}.Audiodeskription"
            title = Title(
                id=item_id,
                kind=TitleKind.EPISODE,
                name=video.series or video.title,
                season=video.season if video.season and video.season > 0 else None,
                episode=video.episode if video.episode and video.episode > 0 else None,
                episode_name=episode_name,
                synopsis=video.synopsis or None,
                duration=video.duration,
                service=self.ID,
            )
        else:
            movie_name = video.title
            if audio_description:
                movie_name = f"{movie_name}.Audiodeskription"
            title = Title(
                id=item_id,
                kind=TitleKind.MOVIE,
                name=movie_name,
                year=video.year or None,
                synopsis=video.synopsis or None,
                duration=video.duration,
                service=self.ID,
            )
        ctx.log(f"{video.title}: {source.line()}", "ok")
        try:
            yield ctx.emit(
                self._playback(title, source, is_live=is_live, item=item, client=client, ctx=ctx)
            )
        except Back:
            return

    def _emit_episode(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        episode: api.Episode,
    ) -> Iterator[Ask]:
        """Return True on Back so the parent cancels the remaining batch."""
        audio_description = (
            episode.audio_description or self._audio_description_mode()
        )
        item: dict[str, Any] | None = None
        try:
            ctx.status(f"Resolving {episode.label}")
            fetch_item = getattr(client, "fetch_item", None)
            if callable(fetch_item):
                item = fetch_item(episode.id, episode.partner)
            source = yield from self._resolve_source(
                ctx,
                client,
                episode.id,
                episode.partner,
                audio_description=audio_description,
                item=item,
            )
        except Back:
            return True
        except api.ArdError as exc:
            ctx.error(f"{episode.label}: {exc}")
            return
        episode_name = episode.title
        if audio_description:
            episode_name = f"{episode_name}.Audiodeskription"
        title = Title(
            id=episode.id,
            kind=TitleKind.EPISODE,
            name=episode.series,
            season=episode.season if episode.season > 0 else None,
            episode=episode.number if episode.number > 0 else None,
            episode_name=episode_name,
            synopsis=episode.synopsis or None,
            duration=api.duration_seconds(item) if item else None,
            service=self.ID,
        )
        ctx.log(f"{episode.label}: {source.line()}", "ok")
        try:
            yield ctx.emit(self._playback(title, source, item=item, client=client, ctx=ctx))
        except Back:
            return True

    def _emit_live(
        self,
        ctx: FlowContext,
        client: api.ArdApi,
        channel: api.Channel,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving live {channel.name}")
            item = client.fetch_item(channel.id, channel.partner)
            video = client.video_from_item(
                item, channel.id, channel.partner, is_live=True
            )
            source = yield from self._resolve_source(
                ctx,
                client,
                channel.id,
                channel.partner,
                is_live=True,
                audio_description=self._audio_description_mode(),
                item=item,
            )
        except Back:
            return
        except api.ArdError as exc:
            ctx.error(f"{channel.name}: {exc}")
            yield from self._live_pause(ctx, f"{channel.name}  ·  {exc}")
            return
        programme = channel.now or (video.title if video.title != channel.name else "")
        if not (yield from self._live_pause(ctx, f"{channel.name}  ·  {programme or 'live stream'}")):
            return
        title = Title(
            id=channel.id,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            episode_name=programme or None,
            duration=video.duration,
            service=self.ID,
        )
        ctx.log(f"{title.name}: {source.line()}", "ok")
        try:
            yield ctx.emit(
                self._playback(title, source, is_live=True, item=item, client=client, ctx=ctx)
            )
        except Back:
            return

    def _live_pause(self, ctx: FlowContext, title: str) -> Iterator[Ask]:
        try:
            answer = yield ctx.confirm(title, default=True)
        except Back:
            return False
        return bool(answer)

    def _playback(
        self,
        title: Title,
        source: api.Source,
        *,
        is_live: bool = False,
        item: dict[str, Any] | None = None,
        client: api.ArdApi | None = None,
        ctx: FlowContext | None = None,
    ) -> Playback:
        item = source.item or item
        is_live = is_live or source.is_live
        if item and title.duration is None and not is_live:
            title = replace(title, duration=api.duration_seconds(item))
        chapters = []
        if item and self.fetch_chapters_enabled() and not is_live:
            chapters = api.extract_chapters(item)
            if client is not None:
                try:
                    chapters = client.chapters(item)
                except api.ArdError as exc:
                    if ctx is not None:
                        ctx.warn(f"ARD chapter metadata unavailable: {exc}")
        attachments = (
            api.extract_attachments(item)
            if item and self.fetch_attachments_enabled()
            else []
        )
        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={
                "User-Agent": api.USER_AGENT,
                "Accept": "*/*",
                "Referer": f"{api.WWW}/",
            },
            proxy=self.ctx.proxy,
            is_live=is_live,
            drm=None,
            chapters=chapters,
            attachments=attachments,
            subtitle_references=api.extract_subtitles(item) if item and not is_live else [],
            note=source.line(),
        )

    def augment_tracks(self, playback: Playback, tracks, log) -> list[StreamInfo]:
        del log
        visible = getattr(tracks, "selectable_streams", ())
        seen = {
            (stream.url, stream.language or "und", stream.extra.get("subtitle_kind") or stream.group_id or "normal")
            for stream in visible if stream.media_type in {"subtitle", "subtitles", "text"}
        }
        rows: list[StreamInfo] = []
        for index, reference in enumerate(playback.subtitle_references, 1):
            identity = (reference.url, reference.language, reference.kind)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(StreamInfo(
                manifest_type="service", media_type="subtitle",
                url=reference.url, original_url=reference.url,
                id=f"ard-subtitle-{index}", group_id=reference.kind,
                name=reference.name, language=reference.language,
                role="Simple Language" if reference.kind == "simple-language" else None,
                codecs="webvtt" if reference.format == "vtt" else "ttml",
                extension=reference.format,
                extra={
                    "service_sidecar": "subtitle",
                    "subtitle_reference_url": reference.url,
                    "subtitle_kind": reference.kind,
                    "forced": reference.kind == "forced",
                    "cc": reference.kind == "cc",
                },
            ))
        return rows

    def prepare_download(self, playback: Playback, log) -> None:
        self.prepare_download_with_progress(playback, log, lambda *_args: None)

    def prepare_download_with_progress(self, playback: Playback, log, progress) -> None:
        selected = [reference for reference in playback.subtitle_references if reference.selected]
        if not selected:
            return
        client = self.client()
        directory = private_directory(self.ctx.subtitle_dir())
        output_format = str(self.settings.get("sub_format", "srt") or "srt")
        auto_fix = bool(self.settings.get("auto_subtitle_fix", True))
        staged = {Path(track.path).resolve() for track in playback.mux_imports if track.path}
        for reference in selected:
            try:
                data = client.subtitle(
                    reference.url,
                    progress=partial(progress, reference),
                )
                identity = f"{reference.url}\0{reference.language}\0{reference.kind}".encode()
                digest = hashlib.sha256(identity + b"\0" + data).hexdigest()[:12]
                stem = re.sub(r"[^\w.-]+", ".", playback.save_name).strip(".")[:140] or "ARD"
                original = directory / f"{stem}.{digest}.{reference.format}"
                atomic_write_bytes(original, data)
                path = convert_subtitle_file(
                    original, output_format=output_format,
                    output_path=original.with_suffix(f".{output_format}"),
                    auto_fix=auto_fix, duration=playback.title.duration,
                )
                if not path.is_file() or not path.stat().st_size:
                    raise SubtitleConversionError("no subtitle output was produced")
                path.chmod(0o600)
            except (api.ArdError, OSError, SubtitleConversionError) as exc:
                raise api.ArdError(
                    f"Could not prepare ARD subtitles ({reference.language}/{reference.kind}): {exc}"
                ) from exc
            if path.resolve() not in staged:
                staged.add(path.resolve())
                playback.mux_imports.append(ExternalTrack(
                    path=str(path), language=reference.language, name=reference.name,
                    kind=reference.kind, original=reference.original,
                    forced=reference.kind == "forced",
                ))
            log(f"ARD subtitle prepared: {reference.language} / {reference.kind}", "ok")


__all__ = ["Ard", "api"]

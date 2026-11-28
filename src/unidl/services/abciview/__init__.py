"""ABC iView - Australia's free public broadcaster."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path

from ...core.attachments import Attachment
from ...core.cdm import CdmError
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext, Pick
from ...core.playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from ...core.qr import QrPresentation
from ...core.secureio import atomic_write_bytes, private_directory, private_file
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from ...downloader.subtitles import SubtitleConversionError, convert_subtitle_file
from . import api

_CODEC = Setting(
    key="codec",
    label="Video codec",
    kind="choice",
    options=[
        Option("auto", "Auto · DASH first, probe 1080p"),
        Option("dash", "DASH"),
        Option("hls", "HLS"),
    ],
    default="auto",
    help=(
        "Selects the ABC iView video delivery family. Auto prefers DASH and "
        "only uses HLS when DASH is absent. DASH probes the 1080p sibling of "
        "a 720p manifest; if unavailable, the original DASH URL is retained."
    ),
)


class ABCiView(Service):
    ID = "abciview"
    NAME = "ABC iView"
    TAG = "iview"
    ALIASES = ("iview", "abc-iview", "abcau", "abc.net.au")
    TITLE_RE = r"(?:iview\.abc\.net\.au/|abc\.net\.au/listen/)"
    GEOFENCE = ("AU",)
    MEDIA_TYPES = ("audio", "video")
    DESCRIPTION = (
        "Australian free VOD, live TV, podcasts and live radio. Anonymous JWT "
        "or Android-TV profile linking with a TV code; AU IP required."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SETTINGS = [_CODEC]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.AbcIviewApi | None = None

    def login_method(self) -> str:
        state = api.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        return api.normalize_login_method(state.login_method)

    def _set_login_method(self, value: str) -> None:
        method = api.normalize_login_method(value)
        state = api.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if method == "anonymous":
            state.clear_account()
        else:
            state.login_method = "tv"
        self._write_state(state)
        self._api = None

    def login_method_label(self) -> str:
        return "TV code" if self.login_method() == "tv" else "Anonymous"

    def codec_mode(self) -> str:
        value = str(self.settings.get("codec") or "auto").strip().lower()
        return value if value in {"auto", "dash", "hls"} else "auto"

    def auth_status(self) -> AuthStatus:
        state = api.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        if self.login_method() == "tv" and state.linked:
            return AuthStatus(
                logged_in=True,
                anonymous_ok=True,
                label=f"TV linked · {state.account_label}",
                detail="ABC Profile TV link cache",
                logout_available=True,
            )
        if self.login_method() == "tv":
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                label="TV not linked",
                detail="anonymous browsing remains available",
            )
        return AuthStatus(
            logged_in=False,
            anonymous_ok=True,
            label="anonymous mode",
            detail="anonymous JWT for DRM",
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        if self.login_method() == "anonymous":
            try:
                ctx.status("Starting ABC iView anonymous session")
                client.anonymous_login()
            except api.AbcIviewError as exc:
                ctx.log(f"profile status unavailable: {exc}", "info")
            self._api = client
            ctx.log("ABC iView anonymous session ready", "ok")
            return

        if client.state.linked:
            self._api = client
            ctx.log(f"ABC iView TV session already linked · {client.state.account_label}", "ok")
            return
        try:
            ctx.status("Starting ABC iView TV-code login")
            challenge = client.start_tv_link()
        except api.AbcIviewError as exc:
            ctx.error(f"ABC iView TV-code login could not start: {exc}")
            return

        try:
            state = yield ctx.wait_for(
                "Link ABC iView on another device",
                [
                    ("Open", challenge.verification_url),
                    ("Code", challenge.code),
                    "Enter the code on the ABC link page. This screen checks automatically.",
                ],
                poll=client.poll_tv_link,
                timeout=challenge.expires_in,
                interval=challenge.interval,
                qr=QrPresentation(
                    payload=challenge.verification_url,
                    fallback_url=challenge.verification_url,
                    alt="ABC iView TV login",
                ),
                hint=challenge.verification_url,
            )
        except Back:
            return
        except api.AbcIviewError as exc:
            ctx.error(f"ABC iView TV-code login failed: {exc}")
            return
        if not isinstance(state, api.SessionState) or not state.linked:
            ctx.error("ABC iView TV-code login finished without a linked account")
            return
        self._api = client
        ctx.log(f"ABC iView TV login confirmed · {state.account_label}", "ok")

    def logout(self) -> None:
        client = self._new_client()
        try:
            client.unlink_tv()
        except api.AbcIviewError:
            client.state.clear_account()
            self._write_state(client.state)
        self._api = None
        super().logout()

    def _write_state(self, state: api.SessionState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def _new_client(self) -> api.AbcIviewApi:
        client = api.AbcIviewApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            state=api.SessionState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE)),
            on_save=self._write_state,
        )
        self._api = client
        return client

    def client(self, ctx: FlowContext | None = None) -> api.AbcIviewApi:
        if self._api is None:
            self._api = self._new_client()
            if ctx is not None:
                try:
                    ctx.status("Checking ABC iView profile status")
                    self._api.anonymous_login()
                except api.AbcIviewError as exc:
                    ctx.log(f"profile status unavailable: {exc}", "info")
        return self._api

    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        while True:
            status = self._auth_status_quietly()
            choices = [
                Choice("VOD - open a URL or content ID", "url"),
                Choice("Live TV", "live"),
                Choice("Search", "search"),
                Choice(
                    f"Login: {self.login_method_label()}",
                    "login_method",
                    detail="Change between anonymous and TV-code mode",
                ),
                Choice("Settings", "settings"),
            ]
            if self.login_method() == "tv":
                choices.append(
                    Choice(
                        "Sign in again" if status.logged_in else "Sign in",
                        "login",
                        detail=status.label,
                    )
                )
            if status.logged_in:
                choices.append(Choice("Sign out", "logout", detail=status.label))
            page = ctx.pick("", choices, scope=SCOPE_ROOT)
            action = yield page
            if action is None:
                return
            page.cursor = next((i for i, choice in enumerate(page.choices) if choice.value == action), 0)
            try:
                if action == "url":
                    yield from self.open_url(ctx, "")
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "search":
                    yield from self.search(ctx, "")
                elif action == "login_method":
                    yield from self.select_login_method(ctx)
                elif action == "settings":
                    yield ctx.settings_request()
                elif action == "login":
                    yield from self.begin_login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("ABC iView TV account signed out; anonymous browsing remains available", "ok")
            except Back:
                pass
            if not ctx.interactive:
                return

    def select_login_method(self, ctx: FlowContext) -> Iterator[Ask]:
        page = ctx.pick(
            "ABC iView login",
            [
                Choice(
                    "Anonymous",
                    "anonymous",
                    detail="Free catalogue and playback; no ABC profile",
                ),
                Choice(
                    "TV code",
                    "tv",
                    detail="Pair at abc.net.au/linktv",
                ),
            ],
            cursor=1 if self.login_method() == "tv" else 0,
        )
        selected = yield page
        if selected is None:
            return
        self._set_login_method(str(selected))
        ctx.log(f"ABC iView login mode: {self.login_method_label()}", "ok")
        if self.login_method() == "tv":
            yield from self.begin_login(ctx)

    def _browse_input(
        self,
        ctx: FlowContext,
        title: str,
        initial: str,
        visit: Callable[[FlowContext, str], Iterator[Ask]],
    ) -> Iterator[Ask]:
        value = str(initial or "").strip()
        page = ctx.text(title)
        while True:
            if not value:
                value = str((yield page) or "").strip()
                if not value:
                    return
            page.default = value
            try:
                yield from visit(ctx, value)
            except Back:
                pass
            if not ctx.interactive:
                return
            value = ""

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        yield from self._browse_input(ctx, "Enter URL or content ID", target, self._open_target)

    def _open_target(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        try:
            parsed = api.parse_input(target)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        if parsed is None:
            ctx.error(
                f"That does not look like an ABC iView link or id: {target}\n"
                "Expected https://iview.abc.net.au/show/<slug>, "
                "https://iview.abc.net.au/video/<id>, an ABC Listen URL, "
                "or a bare show/video id."
            )
            return
        client = self.client(ctx)
        if parsed.kind == "listen_program":
            yield from self._listen_program(ctx, client, parsed.value)
            return
        if parsed.kind == "listen_audiobook":
            yield from self._listen_program(ctx, client, parsed.value)
            return
        if parsed.kind == "listen_episode":
            yield from self._listen_episode(ctx, client, parsed.value)
            return
        if parsed.kind == "listen_live":
            yield from self._listen_live(ctx, client, parsed.value)
            return
        if parsed.kind == "listen_station":
            yield from self._listen_live(ctx, client, parsed.value)
            return
        if parsed.kind == "video":
            yield from self._video(ctx, client, parsed.value)
            return
        yield from self._show(ctx, client, parsed.value)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        yield from self._browse_input(ctx, "Search", query, self._search_results)

    def _search_results(self, ctx: FlowContext, wanted: str) -> Iterator[Ask]:
        client = self.client(ctx)
        try:
            ctx.status(f"Searching ABC iView for {wanted}")
            hits = client.search(wanted)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"ABC iView found nothing for {wanted!r}")
            return
        page = ctx.pick(
            f"ABC iView / Listen  ·  {wanted}  ·  {len(hits)} results",
            [
                Choice(
                    hit.label,
                    hit,
                    detail=hit.description[:90] if hit.description else hit.type_label,
                    tags=(
                        hit.type_label if hit.kind not in {"show", "video", "unknown"} else hit.kind,
                    ) if hit.kind and hit.kind != "unknown" else (),
                )
                for hit in hits
            ],
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            page.cursor = hits.index(chosen)
            try:
                if chosen.kind == "show":
                    yield from self._show(ctx, client, chosen.id)
                elif chosen.kind == "video":
                    yield from self._video(ctx, client, chosen.id)
                elif chosen.kind == "podcast_series":
                    yield from self._listen_program(ctx, client, chosen.url)
                elif chosen.kind in {"podcast_episode", "audiobook_chapter", "radio_replay"}:
                    yield from self._listen_episode(ctx, client, chosen.url)
                elif chosen.kind == "radio":
                    yield from self.radio(ctx, client)
                else:
                    ctx.warn(f"Unsupported search result type: {chosen.type_label or chosen.kind}")
            except Back:
                pass
            if not ctx.interactive:
                return

    def _listen_program(
        self, ctx: FlowContext, client: api.AbcIviewApi, url: str
    ) -> Iterator[Ask]:
        try:
            is_audiobook = "/listen/audiobooks/" in url.casefold()
            ctx.status("Loading ABC audiobook" if is_audiobook else "Loading ABC Listen programme")
            program = client.get_listen_audiobook(url) if is_audiobook else client.get_listen_program(url)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        ctx.log(f"{program.title}: {len(program.episodes)} podcast episode(s)", "ok")
        page = ctx.pick(
            f"{program.title}  ·  {len(program.episodes)} episodes",
            [
                Choice(
                    episode.label,
                    episode,
                    detail=(
                        f"{_format_duration(episode.duration)} · {episode.presenter}"
                        if episode.presenter
                        else _format_duration(episode.duration)
                    ).strip(" ·"),
                    tags=("podcast",),
                )
                for episode in program.episodes
            ],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        while True:
            chosen = yield page
            if not chosen:
                return
            page.preselected = [
                i for i, choice in enumerate(page.choices) if choice.value in chosen
            ]
            if page.preselected:
                page.cursor = page.preselected[0]
            try:
                if len(chosen) > 1:
                    ctx.batch(len(chosen))
                for episode in chosen:
                    yield from self._listen_episode(ctx, client, episode.url)
            except Back:
                pass
            if not ctx.interactive:
                return

    def _listen_episode(
        self, ctx: FlowContext, client: api.AbcIviewApi, url: str
    ) -> Iterator[Ask]:
        try:
            ctx.status("Resolving ABC Listen episode")
            media = client.get_listen_episode(url)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        ctx.log(f"{media.title}: {media.mime or media.codec or 'audio'}", "ok")
        yield ctx.emit(self._audio_playback(media))

    def _listen_live(
        self, ctx: FlowContext, client: api.AbcIviewApi, url: str
    ) -> Iterator[Ask]:
        try:
            ctx.status("Resolving ABC Listen live radio")
            media = client.get_radio_station(url)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        label = f"{media.title} · {media.program}" if media.program else media.title
        ctx.log(f"{label}: live {media.codec or 'audio'}", "ok")
        yield ctx.emit(self._audio_playback(media))

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        category_page = ctx.pick(
            "ABC Live",
            [
                Choice("TV", "tv", detail="ABC iView live channels"),
                Choice("Radio", "radio", detail="ABC Listen live stations"),
            ],
        )
        while True:
            category = yield category_page
            if category is None:
                return
            category_page.cursor = next(
                (i for i, choice in enumerate(category_page.choices) if choice.value == category),
                0,
            )
            try:
                if category == "radio":
                    yield from self.radio(ctx, client)
                else:
                    yield from self._live_tv(ctx, client)
            except Back:
                pass
            if not ctx.interactive:
                return

    def _live_tv(self, ctx: FlowContext, client: api.AbcIviewApi) -> Iterator[Ask]:
        try:
            ctx.status("Loading ABC iView live channels")
            channels = client.get_live_channels()
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("ABC iView published no live channels")
            return
        page = ctx.table(
            f"ABC iView Live  ·  {len(channels)} channels",
            ["Channel", "Collection", "Type"],
            [
                (channel.title, channel.collection or "-", channel.type or "livestream")
                for channel in channels
            ],
            channels,
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            try:
                yield from self._emit_episode(ctx, client, chosen, is_live=True)
            except Back:
                pass
            if not ctx.interactive:
                return

    def radio(
        self, ctx: FlowContext, client: api.AbcIviewApi
    ) -> Iterator[Ask]:
        try:
            ctx.status("Loading ABC Listen radio stations")
            stations = client.get_radio_stations()
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        page = ctx.table(
            f"ABC Radio  ·  {len(stations)} stations",
            ["Station", "Region", "Coverage"],
            [
                (station.title, station.region or "National", station.description or "-")
                for station in stations
            ],
            stations,
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            try:
                yield from self._listen_live(ctx, client, chosen.url)
            except Back:
                pass
            if not ctx.interactive:
                return

    def _show(self, ctx: FlowContext, client: api.AbcIviewApi, show_id: str) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading show {show_id}")
            show = client.get_show(show_id)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return

        if show.unavailable_message:
            ctx.warn(f"{show.title}: {show.unavailable_message}")
            return

        if show.kind == "movie":
            if not show.video_id:
                ctx.error(f"Could not find a video id for {show.title}")
                return
            yield from self._video(ctx, client, show.video_id, movie=show)
            return

        if show.kind == "video" and show.video_id and not show.episodes:
            yield from self._video(ctx, client, show.video_id)
            return

        if not show.episodes:
            if show.video_id:
                yield from self._video(ctx, client, show.video_id)
                return
            ctx.warn(f"ABC iView returned no episodes for {show.title}")
            return

        yield from self._series(ctx, client, show)

    def _series(self, ctx: FlowContext, client: api.AbcIviewApi, show: api.Show) -> Iterator[Ask]:
        seasons: dict[int, list[api.Episode]] = {}
        for episode in show.episodes:
            seasons.setdefault(episode.season, []).append(episode)
        sorted_seasons = sorted(seasons.keys())
        ctx.log(f"{show.title}: {len(show.episodes)} episode(s) in {len(sorted_seasons)} season(s)", "ok")

        if len(sorted_seasons) == 1:
            season_num = sorted_seasons[0]
            yield from self._pick_episodes(ctx, client, self._episode_page(ctx, show.title, season_num, seasons[season_num]))
            return

        page = ctx.pick(
            f"{show.title}  ·  seasons",
            [
                Choice(
                    f"Season {num}" if num else "Episodes",
                    num,
                    detail=f"{len(seasons[num])} episode(s)",
                )
                for num in sorted_seasons
            ],
        )
        episode_pages: dict[int, Pick] = {}
        while True:
            season_num = yield page
            if season_num is None:
                return
            page.cursor = sorted_seasons.index(season_num)
            if season_num not in episode_pages:
                episode_pages[season_num] = self._episode_page(ctx, show.title, season_num, seasons[season_num])
            try:
                yield from self._pick_episodes(ctx, client, episode_pages[season_num])
            except Back:
                pass
            if not ctx.interactive:
                return

    def _episode_page(
        self, ctx: FlowContext, title: str, season_num: int, episodes: list[api.Episode]
    ) -> Pick:
        heading = f"{title}  ·  Season {season_num}" if season_num else f"{title}  ·  episodes"
        return ctx.pick(
            f"{heading}  ·  {len(episodes)} episodes",
            [
                Choice(item.label, item, detail=item.description[:90])
                for item in sorted(episodes, key=lambda item: (item.number, item.name))
            ],
            multi=True,
            hint="space to tick, enter to confirm",
        )

    def _pick_episodes(
        self,
        ctx: FlowContext,
        client: api.AbcIviewApi,
        page: Pick,
    ) -> Iterator[Ask]:
        while True:
            chosen = yield page
            if not chosen:
                return
            page.preselected = [i for i, choice in enumerate(page.choices) if choice.value in chosen]
            if page.preselected:
                page.cursor = page.preselected[0]
            try:
                if len(chosen) > 1:
                    ctx.batch(len(chosen))
                for item in chosen:
                    yield from self._emit_episode(ctx, client, item)
            except Back:
                pass
            if not ctx.interactive:
                return

    def _video(
        self,
        ctx: FlowContext,
        client: api.AbcIviewApi,
        video_id: str,
        *,
        movie: api.Show | None = None,
    ) -> Iterator[Ask]:
        if movie is not None:
            episode = api.Episode(
                id=video_id,
                title=movie.title,
                year=movie.year,
                name=movie.title,
                description=movie.synopsis,
                type="movie",
            )
            yield from self._emit_episode(
                ctx, client, episode, year=movie.year, force_movie=True
            )
            return
        try:
            ctx.status(f"Loading video {video_id}")
            episode = client.get_video(video_id)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        yield from self._emit_episode(ctx, client, episode)

    def _emit_episode(
        self,
        ctx: FlowContext,
        client: api.AbcIviewApi,
        episode: api.Episode,
        *,
        is_live: bool | None = None,
        year: str = "",
        force_movie: bool = False,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {episode.label}")
            source = client.get_source(episode.id, codec=self.codec_mode())
        except api.AbcIviewError as exc:
            ctx.error(f"{episode.label}: {exc}")
            return
        ctx.log(f"{episode.label}: {source.line()}")
        live = bool(is_live if is_live is not None else (episode.is_live or source.is_live))
        title = self._title(episode, year=year, force_movie=force_movie, is_live=live)
        yield ctx.emit(self._playback(title, source, is_live=live))

    def _audio_playback(self, media: api.ListenMedia) -> Playback:
        if media.is_live:
            title = Title(
                id=media.id,
                kind=TitleKind.STATION,
                name=media.title,
                episode_name=media.program or None,
                channel=media.title,
                artist=media.artist or media.title,
                album=media.album or media.title,
                publisher="ABC",
                synopsis=media.description or None,
                cover_url=media.cover_url or None,
                service=self.ID,
            )
        else:
            program = media.program or "ABC Listen"
            title = Title(
                id=media.id,
                kind=TitleKind.TRACK,
                name=program,
                episode_name=media.title or None,
                year=media.year or None,
                duration=media.duration,
                artist=media.artist or program,
                album=media.album or program,
                publisher="ABC",
                synopsis=media.description or None,
                cover_url=media.cover_url or None,
                service=self.ID,
            )

        attachments = []
        if media.cover_url and self.fetch_attachments_enabled():
            attachments.append(
                Attachment(media.cover_url, name="Artwork", kind="artwork")
            )
        note = (
            f"live radio · {media.mime or media.codec or 'audio'}"
            if media.is_live
            else f"podcast · {media.mime or media.codec or 'audio'}"
        )
        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=media.url,
            alternate_manifest_urls=media.alternates,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=media.is_live,
            audio_only=True,
            audio_codec_hint=media.codec,
            audio_tags=title.audio_tags(),
            attachments=attachments,
            drm=DrmInfo(clear=True),
            note=note,
        )

    def _title(
        self,
        episode: api.Episode,
        *,
        year: str = "",
        force_movie: bool = False,
        is_live: bool = False,
    ) -> Title:
        if is_live:
            return Title(
                id=episode.id,
                kind=TitleKind.CHANNEL,
                name=episode.title,
                channel=episode.title,
                episode_name=episode.name if episode.name != episode.title else None,
                synopsis=episode.description or None,
                service=self.ID,
            )
        if force_movie or (
            not episode.season and not episode.number and episode.type in {"movie", "feature", "single"}
        ):
            return Title(
                id=episode.id,
                kind=TitleKind.MOVIE,
                name=episode.title,
                year=year or episode.year or None,
                synopsis=episode.description or None,
                service=self.ID,
            )
        if episode.season or episode.number:
            return Title(
                id=episode.id,
                kind=TitleKind.EPISODE,
                name=episode.title,
                season=episode.season or None,
                episode=episode.number or None,
                episode_name=episode.name or None,
                synopsis=episode.description or None,
                service=self.ID,
            )
        return Title(
            id=episode.id,
            kind=TitleKind.MOVIE,
            name=episode.title,
            year=year or episode.year or None,
            episode_name=episode.name if episode.name != episode.title else None,
            synopsis=episode.description or None,
            service=self.ID,
        )

    def _playback(self, title: Title, source: api.Source, *, is_live: bool = False) -> Playback:
        if source.duration:
            title.duration = source.duration
        live = bool(is_live or source.is_live)
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=is_live,
            chapters=list(source.chapters) if not live and self.fetch_chapters_enabled() else [],
            attachments=list(source.attachments) if self.fetch_attachments_enabled() else [],
            subtitle_references=[] if live else list(source.subtitles),
            note=source.line(),
        )
        if source.protected:
            playback.drm = DrmInfo(
                system="widevine",
                license_url=api.LICENSE_URL,
                headers={"x-keyos-authorization": source.customdata},
                context={
                    "customdata": source.customdata,
                    "video_id": source.video_id,
                },
            )
        else:
            playback.drm = DrmInfo(clear=True)
        return playback

    def augment_tracks(
        self,
        playback: Playback,
        tracks,
        log: Callable[[str], None],
    ) -> list[StreamInfo]:
        """Expose ABC iView API captions alongside manifest tracks in the picker."""
        native_urls = {
            stream.url
            for stream in (getattr(tracks, "streams", ()) or ())
            if stream.media_type in {"subtitle", "subtitles", "text"}
            and not stream.extra.get("portable_subtitle_reference")
        }
        if native_urls and playback.subtitle_references:
            playback.subtitle_references = [
                replace(reference, selected=False) if reference.url in native_urls else reference
                for reference in playback.subtitle_references
            ]
        seen: set[tuple[str, str, str]] = set()
        rows: list[StreamInfo] = []
        for reference in playback.subtitle_references:
            url = str(reference.url or "").strip()
            if not url or url in native_urls:
                continue
            language = reference.language or "und"
            kind = reference.kind or "normal"
            identity = (url, language.casefold(), kind.casefold())
            if identity in seen:
                continue
            seen.add(identity)
            fmt = api.normalize_subtitle_format(reference.format, url=url, default="vtt") or "vtt"
            rows.append(
                StreamInfo(
                    manifest_type="service",
                    media_type="subtitle",
                    url=url,
                    original_url=url,
                    id=f"abciview-subtitle-{len(rows) + 1}",
                    group_id=kind,
                    name=reference.name or language,
                    language=language,
                    role={"sdh": "SDH", "forced": "Forced"}.get(kind),
                    codecs={
                        "vtt": "webvtt",
                        "srt": "subrip",
                        "ttml": "ttml",
                        "dfxp": "ttml",
                        "xml": "ttml",
                    }.get(fmt, fmt),
                    extension=fmt,
                    extra={
                        "service_sidecar": "subtitle",
                        "subtitle_reference_url": url,
                        "subtitle_kind": kind,
                        "subtitle_original": reference.original,
                    },
                )
            )
        if rows and callable(log):
            log(f"ABC iView: added {len(rows)} API subtitle track(s) to the picker")
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
        if not playback.subtitle_references:
            return
        owned = {
            (
                ref.language or "und",
                ref.name or ref.language or "",
                ref.kind or "normal",
            )
            for ref in playback.subtitle_references
        }
        playback.mux_imports[:] = [
            track
            for track in playback.mux_imports
            if (
                track.language or "und",
                track.name or track.language or "",
                track.kind or "normal",
            )
            not in owned
            or Path(track.path).suffix.casefold()
            not in {".srt", ".vtt", ".webvtt", ".ttml", ".dfxp", ".xml", ".ass", ".ssa"}
        ]
        selected = [ref for ref in playback.subtitle_references if ref.selected]
        if not selected:
            return
        client = self._api or self.client()
        try:
            directory = private_directory(self.ctx.subtitle_dir())
        except OSError as exc:
            raise api.AbcIviewError(f"Could not create ABC iView subtitle cache: {exc}") from exc
        output_format = str(self.settings.get("sub_format", "srt") or "srt").strip().lower()
        auto_fix = bool(self.settings.get("auto_subtitle_fix", True))
        staged = {Path(track.path).resolve() for track in playback.mux_imports if track.path}
        duration = playback.title.duration if playback.title else None
        for index, reference in enumerate(selected, 1):
            language = reference.language or "und"
            name = reference.name or language
            kind = reference.kind or "normal"
            source_ext = api.normalize_subtitle_format(reference.format, url=reference.url, default="vtt") or "vtt"
            safe_stem = _safe_stem(f"{playback.save_name}.{language}.{kind}.{index}")
            try:
                report = partial(progress, reference) if progress is not None else None
                data = client.subtitle_bytes(
                    reference.url,
                    headers=reference.headers,
                    progress=report,
                )
                identity = f"{reference.url}\0{language}\0{kind}".encode()
                digest = hashlib.sha256(identity + b"\0" + data).hexdigest()[:12]
                original = directory / f"{safe_stem}.{digest}.{source_ext}"
                atomic_write_bytes(original, data)
                converted = convert_subtitle_file(
                    original,
                    output_format=output_format,
                    output_path=(
                        original.with_suffix(f".{output_format}")
                        if output_format != "raw"
                        else None
                    ),
                    auto_fix=auto_fix,
                    duration=duration,
                )
                if not converted.is_file() or converted.stat().st_size <= 0:
                    raise SubtitleConversionError("no subtitle output was produced")
                private_file(converted)
            except (api.AbcIviewError, OSError, SubtitleConversionError) as exc:
                raise api.AbcIviewError(
                    f"Could not prepare ABC iView subtitle {name}: {exc}"
                ) from exc
            resolved = converted.resolve()
            if resolved not in staged:
                staged.add(resolved)
                playback.mux_imports.append(
                    ExternalTrack(
                        path=str(converted),
                        language=language,
                        name=name,
                        kind=kind,
                        original=reference.original,
                        forced=kind == "forced",
                    )
                )
            if callable(log):
                log(f"ABC iView subtitle prepared: {name}")

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        context = dict(drm.context or {})
        customdata = str(
            context.get("customdata")
            or (drm.headers or {}).get("x-keyos-authorization")
            or ""
        )
        try:
            return self._licence_client().widevine_license(
                challenge,
                customdata=customdata,
                license_url=drm.license_url or api.LICENSE_URL,
                headers={
                    key: value
                    for key, value in (drm.headers or {}).items()
                    if key.lower() != "x-keyos-authorization"
                },
            )
        except api.AbcIviewError as exc:
            raise CdmError(str(exc)) from exc

    def _licence_client(self) -> api.AbcIviewApi:
        """Client reused across a run; built on demand for a late licence call."""
        if self._api is None:
            self._api = api.AbcIviewApi(
                session=self.ctx.session(user_agent=api.USER_AGENT)
            )
        return self._api


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^\w.-]+", ".", str(value or ""), flags=re.UNICODE)
    return re.sub(r"\.{2,}", ".", stem).strip(".")[:140] or "ABCiView"


def _format_duration(value: object) -> str:
    try:
        total = max(0, round(float(value)))
    except (TypeError, ValueError):
        return ""
    if not total:
        return ""
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


registry.register(ABCiView)

__all__ = ["ABCiView", "api"]

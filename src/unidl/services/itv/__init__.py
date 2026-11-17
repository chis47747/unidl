"""ITVX - one native service for the Widevine and PlayReady scripts."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterator

from ...core.cdm import CdmError
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext, Pick
from ...core.playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from ...core.qr import QrPresentation
from ...core.secureio import atomic_write_bytes, private_directory
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from ...downloader.subtitles import SubtitleConversionError, convert_subtitle_file
from . import api, metadata

_PLAYREADY_SOAP_ACTION = (
    '"http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"'
)


class ITV(Service):
    ID = "itv"
    NAME = "ITVX"
    TAG = "ITV"
    ALIASES = ("itvx", "itvhub")
    TITLE_RE = r"(?:www\.)?itv\.com/(?:watch|hub|live)/"
    GEOFENCE = ("GB",)
    DESCRIPTION = "ITVX catalogue, live channels and start-again; Widevine or PlayReady."
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine", "playready")
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = "itv_token.json"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.ItvApi | None = None

    # ------------------------------------------------------------------- auth
    def auth_status(self) -> AuthStatus:
        state = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        now = time.time()
        if state.get("access_token") and int(state.get("token_expires") or 0) > now + 300:
            return AuthStatus(True, "ITVX session active", detail="cache")
        if state.get("refresh_token") and int(state.get("refresh_token_expires") or 0) > now:
            return AuthStatus(True, "ITVX session ready to refresh", detail="cache")
        return AuthStatus(False, "TV code sign-in required", logout_available=bool(state.get("access_token") or state.get("refresh_token")))

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = api.ItvApi(
            session=self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            drm_system=self.drm_system(),
        )
        saved = False
        try:
            ctx.status("Requesting an ITVX TV code")
            challenge = client.start_tv_code()

            def poll():
                try:
                    return client.poll_tv_code(challenge)
                except api.ItvError as exc:
                    return exc

            state = yield ctx.wait_for(
                "Sign in to ITVX",
                [("Open", api.ACTIVATE_URL), ("Code", challenge.code)],
                poll=poll,
                timeout=max(1, challenge.expires_at - time.monotonic()),
                interval=api.TV_CODE_INTERVAL,
                qr=QrPresentation(payload=challenge.qr_url, fallback_url=challenge.qr_url, alt="ITVX TV sign-in"),
            )
            if isinstance(state, api.ItvError):
                raise state
            if not isinstance(state, dict) or not state.get("access_token"):
                raise api.ItvError("ITVX TV pairing did not complete")
            self.ctx.tokens.write(self.TOKEN_FILE, state)
            previous = self._api
            self._api = client
            saved = True
            if previous is not None:
                previous.session.close()
            ctx.log("ITVX TV session saved", "ok")
        except (api.ItvError, OSError) as exc:
            ctx.error(str(exc))
        finally:
            if not saved:
                client.session.close()

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        if self._api is not None:
            self._api.session.close()
        self._api = None

    def client(self, ctx: FlowContext | None = None) -> api.ItvApi:
        chosen = self.drm_system()
        client = self._api
        if client is None or client.drm_system != chosen:
            state = self.ctx.tokens.read(self.TOKEN_FILE) or {}
            client = api.ItvApi(
                session=self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
                state=state,
                drm_system=chosen,
            )
        else:
            state = client.state
        if ctx:
            ctx.status("Opening ITVX session")
        try:
            fresh = client.authenticate()
            if fresh is not state:
                self.ctx.tokens.write(self.TOKEN_FILE, fresh)
                if ctx:
                    ctx.log("ITVX token refreshed", "ok")
        except OSError:
            client.set_state(state)
            if client is not self._api:
                client.session.close()
            raise
        except api.ItvError:
            if client is not self._api:
                client.session.close()
            raise
        if self._api is not None and self._api is not client:
            self._api.session.close()
        self._api = client
        return client

    # ---------------------------------------------------------------- browsing
    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        if self.auth_status().logged_in:
            try:
                self.client(ctx)
            except (api.ItvError, OSError) as exc:
                ctx.error(str(exc))
        cursor = 0
        while True:
            status = self._auth_status_quietly()
            choices = []
            if not status.usable:
                choices.append(Choice("Sign in with TV code", "login", detail=status.label))
            choices.extend([
                Choice("VOD - open a URL or content ID", "url"),
                Choice("Live TV", "live"),
                Choice("Search", "search"),
                Choice("Settings", "settings"),
            ])
            if status.usable:
                choices.append(Choice("Sign in again with TV code", "login", detail=status.label))
            if status.logged_in or status.logout_available:
                choices.append(Choice("Sign out", "logout", detail=status.label))
            action = yield ctx.pick("", choices, cursor=cursor, scope=SCOPE_ROOT)
            cursor = next((index for index, choice in enumerate(choices) if choice.value == action), cursor)
            try:
                if action == "url":
                    yield from self._url_prompt(ctx)
                elif action == "search":
                    yield from self._search_prompt(ctx)
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "settings":
                    yield ctx.settings_request()
                elif action == "login":
                    yield from self.begin_login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("ITVX signed out", "ok")
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        yield from self._url_prompt(ctx, target)

    def _url_prompt(self, ctx: FlowContext, target: str | None = None) -> Iterator[Ask]:
        page = ctx.text("Enter URL or content ID")
        if target is None:
            target = yield page
        while True:
            wanted = str(target or "").strip()
            if not wanted:
                return
            page.default = wanted
            try:
                yield from self._open_target(ctx, wanted)
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return
            target = yield page

    def _open_target(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like an ITVX link or content id: {target}\n"
                "Expected an itv.com/watch or /live URL, or an ITV legacy id."
            )
            return
        try:
            client = self.client(ctx)
            if parsed.kind == "live":
                yield from self._emit_live(ctx, client, client.channel(parsed.value))
            else:
                yield from self._show(
                    ctx,
                    client,
                    client.show(
                        parsed.value,
                        shortform_category=parsed.shortform_category,
                        with_images=self.fetch_attachments_enabled(),
                    ),
                )
        except api.ItvError as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        yield from self._search_prompt(ctx, query)

    def _search_prompt(self, ctx: FlowContext, query: str | None = None) -> Iterator[Ask]:
        page = ctx.text("Search")
        if query is None:
            query = yield page
        while True:
            wanted = str(query or "").strip()
            if not wanted:
                return
            page.default = wanted
            try:
                yield from self._search_results(ctx, wanted)
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return
            query = yield page

    def _search_results(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status(f"Searching ITVX for {query}")
            hits = [hit for hit in client.search(query) if hit.route in {"vod", "live"}]
        except api.ItvError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"ITVX found nothing for {query}")
            return
        page = ctx.pick(
            f"ITVX  ·  {query}",
            [
                Choice(
                    hit.title,
                    hit,
                    detail=hit.synopsis[:90],
                    tags=tuple(
                        value
                        for value in (
                            hit.kind,
                            hit.genre,
                            f"{hit.episode_count} episodes" if hit.episode_count else "",
                        )
                        if value
                    ),
                )
                for hit in hits
            ],
        )
        shows: dict[tuple[str, bool], api.Show] = {}
        while True:
            chosen = yield page
            if chosen is None:
                return
            page.cursor = hits.index(chosen)
            try:
                if chosen.route == "live":
                    yield from self._emit_live(ctx, client, client.channel(chosen.channel_id))
                else:
                    key = (chosen.content_id, self.fetch_attachments_enabled())
                    if key not in shows:
                        shows[key] = client.show(key[0], with_images=key[1])
                    yield from self._show(ctx, client, shows[key])
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _show(self, ctx: FlowContext, client: api.ItvApi, show: api.Show) -> Iterator[Ask]:
        if show.films:
            if len(show.films) == 1:
                yield from self._emit(ctx, client, show.films[0])
            else:
                yield from self._items_page(ctx, client, self._item_picker(ctx, show.title, show.films))
            return
        if not show.seasons:
            ctx.warn(f"ITVX returned no episodes for {show.title}")
            return
        if len(show.seasons) == 1:
            season = show.seasons[0]
            page = self._item_picker(ctx, f"{show.title}  ·  {season.label}", season.episodes)
            yield from self._items_page(ctx, client, page)
            return
        page = ctx.pick(
            f"{show.title}  ·  series",
            [Choice(entry.label, entry) for entry in show.seasons],
        )
        episode_pages: dict[int, Pick] = {}
        while True:
            season = yield page
            if season is None:
                return
            page.cursor = show.seasons.index(season)
            if page.cursor not in episode_pages:
                episode_pages[page.cursor] = self._item_picker(ctx, f"{show.title}  ·  {season.label}", season.episodes)
            try:
                yield from self._items_page(ctx, client, episode_pages[page.cursor])
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _item_picker(self, ctx: FlowContext, title: str, items: list[api.Episode]) -> Pick:
        return ctx.pick(
            title,
            [Choice(item.label, item, detail=item.synopsis[:90]) for item in items],
            multi=True,
            hint="space to tick, enter to confirm",
        )

    def _items_page(
        self, ctx: FlowContext, client: api.ItvApi, page: Pick,
    ) -> Iterator[Ask]:
        while True:
            picked = yield page
            if not picked:
                return
            page.preselected = [index for index, choice in enumerate(page.choices) if choice.value in picked]
            page.cursor = page.preselected[0]
            try:
                yield from self._emit_many(ctx, client, list(picked))
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _emit_many(self, ctx: FlowContext, client: api.ItvApi, items: list[api.Episode]) -> Iterator[Ask]:
        if len(items) > 1:
            ctx.batch(len(items))
        for item in items:
            yield from self._emit(ctx, client, item)

    # ------------------------------------------------------------------- live
    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading ITVX live channels")
            channels = client.channels()
        except api.ItvError as exc:
            ctx.error(str(exc))
            return
        page = ctx.table(
            f"ITVX  ·  {len(channels)} live channels",
            ["Channel", "Now", "Next"],
            [(channel.name, channel.now, channel.next) for channel in channels],
            channels,
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            try:
                yield from self._emit_live(ctx, client, client.channel(chosen.id))
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _emit_live(self, ctx: FlowContext, client: api.ItvApi, channel: api.Channel) -> Iterator[Ask]:
        try:
            if client is self._api:
                client = self.client(ctx)
            ctx.status(f"Resolving {channel.name}")
            source = client.live_source(channel, self.drm_system())
        except api.ItvError as exc:
            ctx.error(str(exc))
            return
        restart = False
        page = None
        if source.start_again:
            page = ctx.pick(
                f"{channel.name}  ·  {channel.now or 'Live'}",
                [Choice("Watch live", "live"), Choice("Restart current programme", "restart")],
            )
        refresh_source = False
        while True:
            if page is not None:
                mode = yield page
                if mode is None:
                    return
                restart = mode == "restart"
                page.cursor = int(restart)
                if refresh_source:
                    try:
                        if client is self._api:
                            client = self.client(ctx)
                        channel = client.channel(channel.id)
                        source = client.live_source(channel, self.drm_system())
                        page.title = f"{channel.name}  ·  {channel.now or 'Live'}"
                    except api.ItvError as exc:
                        ctx.error(str(exc))
                        if not ctx.interactive:
                            return
                        continue
                if restart and not source.start_again:
                    ctx.warn("ITVX no longer offers restart for this programme")
                    if not ctx.interactive:
                        return
                    continue
            selected_source = source
            title = Title(
                id=channel.id,
                kind=TitleKind.CHANNEL,
                name=channel.name,
                channel=channel.name,
                episode_name=channel.now or None,
                synopsis=channel.synopsis or None,
                service=self.ID,
            )
            if restart:
                selected_source = api.Source(
                    manifest=source.start_again, license_url=source.license_url, encrypted=source.encrypted,
                    protocol=source.protocol, subtitles=source.subtitles,
                )
            playback = self._playback(title, selected_source, is_live=True, restart=restart)
            if self.fetch_attachments_enabled():
                playback.attachments = metadata.attachments((channel.logo_url, "Channel logo", "image"))
            if page is None:
                yield ctx.emit(playback)
                return
            try:
                yield ctx.emit(playback)
            except Back:
                pass
            except (RuntimeError, OSError) as exc:
                ctx.error(str(exc))
            refresh_source = True
            if not ctx.interactive:
                return

    # --------------------------------------------------------------- playback
    def _emit(self, ctx: FlowContext, client: api.ItvApi, item: api.Episode) -> Iterator[Ask]:
        try:
            if client is self._api:
                client = self.client(ctx)
            ctx.status(f"Resolving {item.label}")
            source = client.source(item, self.drm_system())
        except api.ItvError as exc:
            ctx.error(f"{item.label}: {exc}")
            return
        title = Title(
            id=item.id,
            kind=TitleKind.MOVIE if item.film else TitleKind.EPISODE,
            name=(
                item.title
                if item.film or (item.season is None and item.number is None)
                else item.show_title
            ),
            year=item.year or None,
            season=None if item.film else item.season,
            episode=None if item.film else item.number,
            episode_name=None,
            genre=item.genre or None,
            synopsis=item.synopsis or None,
            service=self.ID,
        )
        playback = self._playback(title, source)
        if self.fetch_attachments_enabled():
            playback.attachments = metadata.attachments(
                (item.image_url, "Title artwork", "artwork"),
                (item.programme_image_url, "Programme artwork", "artwork"),
                (item.poster_url, "Poster", "poster"),
            )
        yield ctx.emit(playback)

    def augment_tracks(self, playback: Playback, tracks, log):
        del tracks, log
        return [
            StreamInfo(
                manifest_type="service",
                media_type="subtitle",
                url=reference.url,
                original_url=reference.url,
                id=f"itv-subtitle-{index}",
                group_id=reference.kind,
                name=reference.name or reference.language,
                language=reference.language,
                codecs="webvtt",
                extension="vtt",
                extra={
                    "service_sidecar": "subtitle",
                    "subtitle_reference_url": reference.url,
                    "subtitle_kind": reference.kind,
                },
            )
            for index, reference in enumerate(playback.subtitle_references, 1)
        ]

    def prepare_download(self, playback: Playback, log) -> None:
        selected = [reference for reference in playback.subtitle_references if reference.selected]
        if not selected:
            return
        client = self._api or self.client()
        directory = private_directory(self.ctx.subtitle_dir())
        safe_name = _safe_component(playback.save_name) or "ITVX"
        output_format = str(self.settings.get("sub_format", "srt") or "srt")
        auto_fix = bool(self.settings.get("auto_subtitle_fix", True))
        for reference in selected:
            subtitle = api.Subtitle(
                url=reference.url,
                language=reference.language.split("-", 1)[0],
                label=reference.name or reference.language,
                locale=reference.language,
            )
            locale = _safe_component(subtitle.locale.replace("-", "_")) or "und"
            try:
                data = client.subtitle_bytes(subtitle)
                digest = hashlib.sha256(subtitle.url.encode() + b"\0" + data).hexdigest()[:12]
                original = directory / f"{safe_name}_{locale}_{digest}.vtt"
                atomic_write_bytes(original, data)
            except (api.ItvError, OSError) as exc:
                log(f"could not stage {subtitle.label} subtitles: {exc}", "warning")
                continue
            try:
                converted = convert_subtitle_file(
                    original,
                    output_format=output_format,
                    output_path=original.with_suffix(f".{output_format}"),
                    auto_fix=auto_fix,
                )
                if not converted.is_file() or converted.stat().st_size == 0:
                    raise SubtitleConversionError("no subtitle output was produced")
            except (SubtitleConversionError, OSError) as exc:
                log(f"could not convert {subtitle.label} subtitles: {exc}", "warning")
                continue
            log(f"subtitle prepared · {subtitle.label}", "ok")
            if not any(track.path == str(converted) for track in playback.mux_imports):
                playback.mux_imports.append(
                    ExternalTrack(
                        path=str(converted),
                        language=subtitle.language or "und",
                        name=subtitle.label,
                    )
                )

    def _playback(
        self,
        title: Title,
        source: api.Source,
        *,
        is_live: bool = False,
        restart: bool = False,
    ) -> Playback:
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT, "Origin": api.WWW, "Referer": f"{api.WWW}/"},
            proxy=self.ctx.proxy,
            is_live=is_live,
            note=f"{source.line()}{' · restart' if restart else ''}",
            subtitle_references=[
                SubtitleReference(
                    url=subtitle.url,
                    language=subtitle.locale,
                    name=subtitle.label,
                )
                for subtitle in source.subtitles
            ],
        )
        playback.drm = (
            DrmInfo(system=self.drm_system(), license_url=source.license_url)
            if source.encrypted
            else DrmInfo(clear=True)
        )
        return playback

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        client = self._api
        if client is None:
            raise CdmError("ITVX session is not open")
        try:
            response = client.session.post(
                drm.license_url,
                data=challenge,
                headers={"Content-Type": "application/octet-stream", "User-Agent": api.USER_AGENT},
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            raise CdmError(f"ITVX Widevine licence failed: {exc}") from exc
        return response.content

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        client = self._api
        if client is None:
            raise CdmError("ITVX session is not open")

        try:
            response = client.session.post(
                drm.license_url,
                data=challenge,
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPAction": _PLAYREADY_SOAP_ACTION,
                    "User-Agent": api.USER_AGENT,
                },
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            raise CdmError(f"ITVX PlayReady licence failed: {exc}") from exc
        return response.text


def _safe_component(value: str) -> str:
    return re.sub(r"[^\w.-]+", "_", value or "").strip("._")


registry.register(ITV)

__all__ = ["ITV", "api"]

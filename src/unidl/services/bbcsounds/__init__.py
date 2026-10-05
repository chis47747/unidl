"""BBC Sounds - the reference audio-only service.

"""

from __future__ import annotations

from collections.abc import Iterator

from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext
from ...core.playback import Playback
from ...core.service import AuthStatus, Capabilities, Service
from ...core.service import registry as _registry
from ...core.titles import Title, TitleKind
from . import api


@_registry.register
class BBCSounds(Service):
    ID = "bbcsounds"
    TAG = "SNDS"
    NAME = "BBC Sounds"
    MEDIA_TYPES = ("audio",)
    ALIASES = ("sounds", "bbc-sounds", "bbcradio")
    TITLE_RE = r"bbc\.(?:co\.uk|com)/sounds"
    GEOFENCE = ("GB",)
    DESCRIPTION = "BBC radio and podcasts. Audio only, no sign-in, no DRM."

    USES = Capabilities()
    SUPPORTS_URL = True
    SUPPORTS_LIVE = True
    SUPPORTS_SEARCH = False
    SUPPORTS_LIBRARY = False

    SETTINGS = []

    # ------------------------------------------------------------------- auth
    def auth_status(self) -> AuthStatus:
        return AuthStatus(anonymous_ok=True, label="no sign-in needed")

    def client(self) -> api.SoundsApi:
        return api.SoundsApi(
            session=self.ctx.session(
                user_agent=api.USER_AGENT,
                headers={"Accept": "application/json"},
                cookies=False,
            )
        )

    # ---------------------------------------------------------------- browsing
    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        while True:
            action = yield ctx.pick(
                "",
                [
                    Choice("Programme or episode - paste a URL or PID", "url"),
                    Choice("Live radio", "live"),
                    Choice("Settings", "settings"),
                ],
                scope=SCOPE_ROOT,
            )
            try:
                if action == "url":
                    target = yield ctx.text(
                        "BBC Sounds URL or PID",
                        placeholder="bbc.co.uk/sounds/play/m001abcd, or m001abcd",
                    )
                    yield from self.open_url(ctx, str(target).strip())
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "settings":
                    yield ctx.settings_request()
            except Back:
                continue
            except RuntimeError as exc:
                # Same rule as Service.home: a refusal ends the step, not the session.
                # core/service.py says why RuntimeError is the line and TypeError is not.
                ctx.problem(
                    f"{self.NAME} could not finish that",
                    f"{type(exc).__name__}: {exc}",
                    "The service menu is still open - the rest of the session is fine.",
                )
                continue

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(f"That does not look like a BBC Sounds link or PID: {target}")
            return

        client = self.client()
        if parsed.kind == "live":
            yield from self._emit_live(ctx, client, parsed.value)
            return
        if parsed.kind == "container":
            yield from self._browse_container(ctx, client, parsed.value)
            return

        # a bare PID could be either; try the episode it most likely is, and fall
        # back to treating it as a series rather than reporting "not found"
        ctx.status(f"Looking up {parsed.value}")
        try:
            item = client.episode(parsed.value)
        except api.SoundsError as exc:
            ctx.warn(f"{exc}; trying it as a series")
            yield from self._browse_container(ctx, client, parsed.value)
            return
        yield from self._emit_item(ctx, client, item)

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client()
        ctx.status("Loading stations")
        stations = client.stations()
        if not stations:
            ctx.warn("BBC Sounds returned no live stations")
            return

        chosen = yield ctx.table(
            "Live radio",
            ["Station", "Now"],
            [(station.programme, station.episode) for station in stations],
            stations,
        )
        target = chosen[0] if isinstance(chosen, list) else chosen
        if target is not None:
            yield from self._emit_live(ctx, client, target.id, station=target)

    # ----------------------------------------------------------------- listings
    def _browse_container(self, ctx: FlowContext, client: api.SoundsApi, container_id: str) -> Iterator[Ask]:
        offset = 0
        while True:
            ctx.status(f"Loading episodes {offset + 1}+")
            items, total = client.container(container_id, offset=offset)
            if not items:
                ctx.warn("No episodes available")
                return

            choices = [Choice(item.episode, item, detail=self._detail(item)) for item in items]
            more = offset + len(items) < total
            if more:
                choices.append(Choice(f"Next {api.PAGE_SIZE} of {total}...", "__more__", navigates=True))

            picked = yield ctx.pick(
                f"{items[0].programme}  ·  {total} episodes",
                choices,
                multi=True,
                hint="space to tick, enter to confirm",
            )
            wanted = [item for item in (picked or []) if item != "__more__"]
            paging = any(item == "__more__" for item in (picked or []))

            for item in wanted:
                yield from self._emit_item(ctx, client, item)
            if not paging:
                return
            offset += api.PAGE_SIZE

    @staticmethod
    def _detail(item: api.SoundsItem) -> str:
        bits = [item.release_date, _duration(item.duration), item.synopsis]
        return "  ·  ".join(bit for bit in bits if bit)

    # ----------------------------------------------------------------- playback
    def _emit_item(self, ctx: FlowContext, client: api.SoundsApi, item: api.SoundsItem) -> Iterator[Ask]:
        ctx.status(f"Resolving {item.episode}")
        try:
            source = client.source_for(item)
        except api.SoundsError as exc:
            ctx.error(f"{item.episode}: {exc}")
            return
        yield ctx.emit(self._playback(item, source))

    def _emit_live(
        self,
        ctx: FlowContext,
        client: api.SoundsApi,
        service_id: str,
        station: api.SoundsItem | None = None,
    ) -> Iterator[Ask]:
        ctx.status(f"Resolving {service_id}")
        try:
            source = client.live_source_for(service_id)
        except api.SoundsError as exc:
            ctx.error(f"{service_id}: {exc}")
            return
        if station is None:
            # A `live:` deep link arrives as a bare id, so the listing is where
            # the station's name and what is on it come from. Without this the
            # file was named after the id and called "Live": bbc_radio_one.Live.
            station = next((s for s in client.stations() if s.id == service_id), None)
        item = station or api.SoundsItem(id=service_id, programme=service_id, episode="Live", live=True)
        item.live = True
        yield ctx.emit(self._playback(item, source))

    def _playback(self, item: api.SoundsItem, source: api.AudioSource) -> Playback:
        title = Title(
            # STATION and TRACK are what make this audio-only. Nothing below sets
            # audio_only itself; the kind carries it.
            id=item.id,
            kind=TitleKind.STATION if item.live else TitleKind.TRACK,
            name=item.programme,
            episode_name=item.episode if item.episode != item.programme else None,
            year=(item.release_date or "")[:4] or None,
            duration=item.duration,
            channel=item.network or None,
            artist=item.programme,
            album=item.album,
            genre="Podcast" if not item.live else "Radio",
            publisher="BBC",
            synopsis=item.synopsis,
            cover_url=item.image_url.replace("{recipe}", "1024x1024"),
            service=self.ID,
        )
        note = f"{source.quality}"
        if source.direct_file:
            note += " · progressive download, already MP3"
        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.url,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            note=note,
        )


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, _ = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


__all__ = ["BBCSounds", "api"]

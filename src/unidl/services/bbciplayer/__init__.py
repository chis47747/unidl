"""BBC iPlayer - programme catalogue, search, regional live TV and UHD."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator

from ...core.flow import Ask, Choice, FlowContext
from ...core.helpers import SUBBY, Helper, HelperError, HelperKind
from ...core.playback import DrmInfo, ExternalTrack, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_RESOLUTION = Setting(
    "source_resolution",
    "Playback source",
    options=[
        Option("auto", "Best available (4K → 1080p → 720p)"),
        Option("4k", "4K only"),
        Option("1080p", "1080p only"),
        Option("720p", "720p only"),
    ],
    default="auto",
    help="This picks the BBC media-selector profile; the shared quality setting still chooses tracks inside it.",
)
_REGION = Setting(
    "live_region",
    "BBC One region",
    options=[
        Option("london", "London"),
        Option("south", "South"),
        Option("south_east", "South East"),
        Option("east", "East"),
        Option("east_midlands", "East Midlands"),
        Option("west_midlands", "West Midlands"),
        Option("west", "West"),
        Option("south_west", "South West"),
        Option("channel_islands", "Channel Islands"),
        Option("yorkshire", "Yorkshire"),
        Option("east_yorkshire", "East Yorkshire & Lincolnshire"),
        Option("north_east", "North East & Cumbria"),
        Option("north_west", "North West"),
        Option("scotland", "Scotland"),
        Option("wales", "Wales"),
        Option("northern_ireland", "Northern Ireland"),
    ],
    default="london",
)
_CURL = Helper(
    key="curl",
    label="curl",
    kind=HelperKind.BINARY,
    candidates=("curl",),
    required=False,
    install_hint="install curl, or set helpers.curl in unidl.yaml",
    degrades_to="4K media selection is unavailable; 1080p and 720p still work",
)
_CERTIFICATE = Helper(
    key="iplayer.pem",
    label="BBC iPlayer UHD client certificate",
    kind=HelperKind.ASSET,
    candidates=("iplayer.pem",),
    required=False,
    install_hint="place iplayer.pem in helpers/bbc/iplayer.pem or set helpers.bbc.iplayer.pem",
    degrades_to="4K media selection is unavailable; 1080p and 720p still work",
)


class BBCiPlayer(Service):
    ID = "bbc"
    NAME = "BBC iPlayer"
    TAG = "iP"
    ALIASES = ("iplayer", "bbciplayer", "bbc-tv")
    TITLE_RE = r"(?:www\.)?bbc\.(?:co\.uk|com)/(?:iplayer/(?:episode|episodes|live)/|programmes/)"
    GEOFENCE = ("GB",)
    DESCRIPTION = "BBC television on demand and regional live channels, clear up to UHD."
    USES = Capabilities()
    SETTINGS = [_RESOLUTION, _REGION]
    HELPERS = [_CURL, _CERTIFICATE, SUBBY]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    def auth_status(self) -> AuthStatus:
        return AuthStatus(False, "no sign-in needed", anonymous_ok=True)

    def client(self, ctx: FlowContext | None = None) -> api.IPlayerApi:
        resolution = str(self.settings.get("source_resolution") or "auto")
        uhd_ready = self.ctx.has_helper("curl") and self.ctx.has_helper("iplayer.pem")
        secure = self._secure(ctx) if uhd_ready else None
        if resolution == "4k" and secure is None:
            missing = []
            if not self.ctx.has_helper("curl"):
                missing.append("curl")
            if not self.ctx.has_helper("iplayer.pem"):
                missing.append("helpers/bbc/iplayer.pem")
            raise api.IPlayerError(
                f"BBC 4K selection needs {' and '.join(missing)}; install them or choose Auto/1080p/720p"
            )
        return api.IPlayerApi(
            session=self.ctx.session(user_agent=api.USER_AGENT_UHD if resolution in {"auto", "4k"} else api.USER_AGENT),
            resolution=resolution,
            region=str(self.settings.get("live_region") or "london"),
            secure=secure,
        )

    def _secure(self, ctx: FlowContext | None = None):
        curl = self.ctx.helper("curl")
        certificate = self.ctx.helper("iplayer.pem")

        def fetch(vpid: str, mediaset: str) -> dict | None:
            runner = self.ctx.runner(log=ctx.log if ctx else None)
            try:
                result = runner.run(
                    [
                        curl,
                        "-sS",
                        "--max-time",
                        "20",
                        "--cert",
                        certificate,
                        "--key",
                        certificate,
                        "-H",
                        f"User-Agent: {api.USER_AGENT_UHD}",
                        api.SECURE_SELECTOR.format(vpid=vpid, mediaset=mediaset),
                    ],
                    timeout=25,
                )
                return api.parse_selector(result.stdout)
            except (HelperError, api.IPlayerError):
                return None

        return fetch

    # ---------------------------------------------------------------- browsing
    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like a BBC iPlayer link or PID: {target}\n"
                "Expected /iplayer/episode|episodes|live/<pid>, /programmes/<pid>, or a BBC PID."
            )
            return
        try:
            client = self.client(ctx)
            if parsed.kind == "programme":
                yield from self._show(ctx, client, client.show(parsed.pid), parsed.series_id)
            elif parsed.kind == "episode":
                yield from self._emit_episode(ctx, client, client.episode(parsed.pid))
            elif parsed.kind == "live":
                yield from self._emit_live(ctx, client, client.channel(parsed.pid))
            else:
                try:
                    item = client.episode(parsed.pid)
                except api.IPlayerError:
                    yield from self._show(ctx, client, client.show(parsed.pid), parsed.series_id)
                else:
                    yield from self._emit_episode(ctx, client, item)
        except api.IPlayerError as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status(f"Searching BBC iPlayer for {query}")
            hits = client.search(query)
        except api.IPlayerError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"BBC iPlayer found nothing for {query}")
            return
        chosen = yield ctx.pick(
            f"BBC iPlayer  ·  {query}",
            [
                Choice(
                    hit.title,
                    hit,
                    detail=hit.synopsis[:90],
                    tags=tuple(value for value in (hit.kind, hit.category) if value),
                )
                for hit in hits
            ],
        )
        if chosen is not None:
            yield from self.open_url(ctx, chosen.url)

    def _show(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        show: api.Show,
        selected: str = "",
    ) -> Iterator[Ask]:
        if not show.seasons:
            ctx.warn(f"BBC returned no seasons for {show.title}")
            return
        season = next((entry for entry in show.seasons if entry.id == selected), None) if selected else None
        if season is None:
            season = (
                show.seasons[0]
                if len(show.seasons) == 1
                else (
                    yield ctx.pick(
                        f"{show.title}  ·  seasons",
                        [Choice(entry.label, entry) for entry in show.seasons],
                    )
                )
            )
        if season is None:
            return
        try:
            ctx.status(f"Loading {show.title}  ·  {season.title}")
            season = client.season(season)
        except api.IPlayerError as exc:
            ctx.error(str(exc))
            return
        if not season.episodes:
            ctx.warn(f"BBC returned no episodes for {season.title}")
            return
        picked = yield ctx.pick(
            f"{show.title}  ·  {season.label}",
            [Choice(item.label, item, detail=item.synopsis[:90]) for item in season.episodes],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        episodes = list(picked or [])
        if len(episodes) > 1:
            ctx.batch(len(episodes))
        for item in episodes:
            yield from self._emit_episode(ctx, client, item)

    # ------------------------------------------------------------------- live
    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading BBC live channels")
            channels = client.channels()
        except api.IPlayerError as exc:
            ctx.error(str(exc))
            return
        chosen = yield ctx.table(
            f"BBC iPlayer  ·  {len(channels)} live channels",
            ["Channel", "Now", "Next"],
            [(channel.name, channel.now, channel.next) for channel in channels],
            channels,
        )
        if chosen is not None:
            variants = client.regional_variants(chosen)
            if len(variants) > 1:
                chosen = yield ctx.pick(
                    f"{chosen.name}  ·  regions",
                    [
                        Choice(
                            variant.region or variant.name,
                            variant,
                            detail=variant.id,
                        )
                        for variant in variants
                    ],
                )
            if chosen is not None:
                # The region picker intentionally skips all schedules. Refresh
                # only the selected feed so its current programme can drive UHD
                # version discovery and the recording label.
                chosen = client.channel(chosen.id)
                yield from self._emit_live(ctx, client, chosen)

    def _emit_live(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        channel: api.Channel,
    ) -> Iterator[Ask]:
        title = Title(
            id=channel.id,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            episode_name=channel.now or None,
            synopsis=channel.synopsis or None,
            service=self.ID,
        )
        yield from self._emit(ctx, client, title, channel, is_live=True)

    # --------------------------------------------------------------- playback
    def _emit_episode(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        item: api.Episode,
    ) -> Iterator[Ask]:
        episodic = not item.film
        title = Title(
            id=item.id,
            kind=TitleKind.EPISODE if episodic else TitleKind.MOVIE,
            name=item.title,
            year=(item.year or None) if item.film else None,
            season=item.season if episodic else None,
            episode=item.number if episodic else None,
            episode_name=item.name if episodic else None,
            genre=item.category or None,
            synopsis=item.synopsis or None,
            service=self.ID,
        )
        yield from self._emit(ctx, client, title, item, is_live=item.live)

    def _emit(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        title: Title,
        item: api.Episode | api.Channel,
        *,
        is_live: bool,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {title.label()}")
            source = client.source(item)
        except api.IPlayerError as exc:
            ctx.error(f"{title.label()}: {exc}")
            return
        save_name = self.save_name(title)
        imports = self._subtitle_imports(ctx, client, source, save_name, is_live=is_live)
        playback = Playback(
            title=title,
            save_name=save_name,
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT_UHD},
            proxy=self.ctx.proxy,
            is_live=is_live,
            drm=DrmInfo(clear=True),
            mux_imports=imports,
            note=source.line(),
        )
        yield ctx.emit(playback)

    def _subtitle_imports(
        self,
        ctx: FlowContext,
        client: api.IPlayerApi,
        source: api.Source,
        save_name: str,
        *,
        is_live: bool,
    ) -> list[ExternalTrack]:
        if is_live or not source.subtitle:
            return []
        try:
            data = client.subtitle(source.subtitle)
            directory = self.ctx.subtitle_dir()
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        except (api.IPlayerError, OSError) as exc:
            ctx.warn(f"subtitle skipped: {exc}")
            return []

        stem = re.sub(r"[^\w.-]+", ".", save_name, flags=re.UNICODE)
        stem = re.sub(r"\.{2,}", ".", stem).strip(".")[:180] or "BBC.iPlayer"
        clean_url = source.subtitle.lower().split("?", 1)[0]
        suffix = next(
            (value for value in (".srt", ".vtt", ".ttml", ".dfxp", ".xml") if clean_url.endswith(value)),
            ".ttml" if b"<tt" in data[:1000].lower() else ".xml",
        )
        digest = hashlib.sha256(source.subtitle.encode("utf-8") + b"\0" + data).hexdigest()[:12]
        original = directory / f"{stem}.en.{digest}{suffix}"
        try:
            if not original.exists() or original.read_bytes() != data:
                original.write_bytes(data)
            os.chmod(original, 0o600)
        except OSError as exc:
            ctx.warn(f"subtitle skipped: {exc}")
            return []

        selected = original
        if suffix != ".srt" and self.ctx.has_helper("subby"):
            converted = original.with_suffix(".srt")
            try:
                self.ctx.runner(log=ctx.log).run(
                    [self.ctx.helper("subby"), "convert", original, "-o", converted, "-l", "en"],
                    timeout=30,
                )
                if not converted.is_file() or converted.stat().st_size == 0:
                    raise HelperError("subby produced no SRT output")
                os.chmod(converted, 0o600)
                selected = converted
                ctx.log("subtitle converted to SRT with subby", "ok")
            except (HelperError, OSError) as exc:
                ctx.warn(f"subby conversion failed; keeping original subtitle: {exc}")
        else:
            ctx.log(f"subtitle staged · {suffix.lstrip('.')}", "ok")
        return [ExternalTrack(path=str(selected), language="en", name="English")]


registry.register(BBCiPlayer)

__all__ = ["BBCiPlayer", "api"]

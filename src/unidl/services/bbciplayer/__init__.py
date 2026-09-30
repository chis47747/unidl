"""BBC iPlayer - programme catalogue, search, regional live TV and UHD."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator

from ...core.chapters import Chapter
from ...core.flow import Ask, Choice, FlowContext
from ...core.playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from . import api

_RESOLUTION = Setting(
    "source_resolution",
    "Playback source",
    options=[
        Option("auto", "Best available · UHD → FHD → HD"),
        Option("uhd", "UHD · 2160p"),
        Option("fhd", "FHD · 1080p"),
        Option("hd", "HD · 720p"),
    ],
    default="auto",
    help=(
        "Chooses the BBC media-selector source profile. This is separate from the "
        "shared video quality setting, which selects tracks inside that source."
    ),
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
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    def auth_status(self) -> AuthStatus:
        return AuthStatus(False, "no sign-in needed", anonymous_ok=True)

    def client(self, ctx: FlowContext | None = None) -> api.IPlayerApi:
        return api.IPlayerApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            resolution=str(self.settings.get("source_resolution") or "auto"),
            region=str(self.settings.get("live_region") or "london"),
        )


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
            sources = client.sources(item)
        except api.IPlayerError as exc:
            ctx.error(f"{title.label()}: {exc}")
            return
        source = sources[0]
        save_name = self.save_name(title)
        chapters = [
            Chapter(
                start_ms=max(0, int(value.get("start_ms") or 0)),
                title=str(value.get("title") or value.get("kind") or "Chapter"),
                kind=str(value.get("kind") or ""),
            )
            for value in source.chapters
            if isinstance(value, dict)
        ]
        drm = DrmInfo(clear=True)
        drm.context["bbc_manifest_sources"] = tuple(sources[1:])
        playback = Playback(
            title=title,
            save_name=save_name,
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=is_live,
            drm=drm,
            subtitle_references=self._subtitle_references(sources, is_live=is_live),
            chapters=chapters,
            merge_manifests=not is_live and len(sources) > 1,
            note=" + ".join(value.line() for value in sources),
        )
        yield ctx.emit(playback)

    def manifest_variants(self, playback: Playback, log) -> list[Playback]:
        if playback.is_live or not playback.merge_manifests or playback.drm is None:
            return []
        sources = playback.drm.context.get("bbc_manifest_sources")
        if not isinstance(sources, tuple):
            return []
        variants: list[Playback] = []
        for source in sources:
            if not isinstance(source, api.Source):
                continue
            variants.append(
                Playback(
                    title=playback.title,
                    save_name=playback.save_name,
                    manifest_url=source.manifest,
                    headers=dict(playback.headers),
                    proxy=playback.proxy,
                    drm=DrmInfo(clear=True),
                    merge_manifests=False,
                    note=source.line(),
                )
            )
            log(f"BBC iPlayer merged source: {source.line()}")
        return variants

    def _subtitle_references(
        self,
        sources: list[api.Source],
        *,
        is_live: bool,
    ) -> list[SubtitleReference]:
        if is_live:
            return []
        references: list[SubtitleReference] = []
        seen: set[str] = set()
        for source in sources:
            url = str(source.subtitle or "").strip()
            if not url:
                continue
            key = url.split("?", 1)[0].casefold()
            if key in seen:
                continue
            seen.add(key)
            references.append(
                SubtitleReference(
                    url=url,
                    language="en",
                    kind="sdh",
                    name="English",
                )
            )
        return references

    def augment_tracks(self, playback: Playback, tracks, log):
        rows: list[StreamInfo] = []
        seen: set[str] = set()
        for index, reference in enumerate(playback.subtitle_references, 1):
            url = str(reference.url or "").strip()
            if not url:
                continue
            key = url.split("?", 1)[0].casefold()
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                StreamInfo(
                    manifest_type="service",
                    media_type="subtitle",
                    url=url,
                    original_url=url,
                    id=f"bbc-subtitle-{index}",
                    group_id=reference.kind or "sdh",
                    name=reference.name or "English",
                    language=reference.language or "en",
                    role="SDH" if (reference.kind or "").casefold() == "sdh" else None,
                    codecs="subrip",
                    extension="srt",
                    extra={
                        "service_sidecar": "subtitle",
                        "subtitle_reference_url": url,
                        "subtitle_kind": reference.kind or "sdh",
                    },
                )
            )
        if rows:
            log(f"BBC iPlayer: added {len(rows)} API subtitle track(s) to the picker")
        return rows

    def prepare_download(self, playback: Playback, log) -> None:
        selected = [subtitle for subtitle in playback.subtitle_references if subtitle.selected]
        if not selected:
            return
        client = self.client()
        directory = self.ctx.subtitle_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        imports: list[ExternalTrack] = []
        for index, subtitle in enumerate(selected, 1):
            data = client.subtitle(subtitle.url)
            clean_url = subtitle.url.lower().split("?", 1)[0]
            suffix = next(
                (value for value in (".srt", ".vtt", ".ttml", ".dfxp", ".xml") if clean_url.endswith(value)),
                ".ttml" if b"<tt" in data[:1000].lower() else ".xml",
            )
            digest = hashlib.sha256(subtitle.url.encode("utf-8") + b"\0" + data).hexdigest()[:12]
            stem = re.sub(
                r"[^\w.-]+",
                ".",
                f"{playback.save_name}.{subtitle.language}.{subtitle.kind}.{index}.{digest}",
            )
            stem = re.sub(r"\.{2,}", ".", stem).strip(".")[:180] or "BBC.iPlayer"
            original = directory / f"{stem}{suffix}"
            if not original.exists() or original.read_bytes() != data:
                original.write_bytes(data)
                os.chmod(original, 0o600)
            selected_path = original
            imports.append(ExternalTrack(str(selected_path), subtitle.language or "en", subtitle.name or "English"))
        playback.mux_imports.extend(imports)
        log(f"BBC iPlayer subtitles: prepared {len(imports)}/{len(selected)} track(s)")


registry.register(BBCiPlayer)

__all__ = ["BBCiPlayer", "api"]

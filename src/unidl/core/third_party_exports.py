"""Adapters from third-party export documents to UniDL's neutral import model.

Third-party files are data, never service sessions.  An adapter may retain the
source service name for output naming, but it must not instantiate that service,
load its account state, or turn an incomplete export into a new licence request.
The TUI enforces that boundary from :attr:`Document.source_format`.

Add future formats to :func:`load` as another narrow detector and converter.  Do
not make UniDL's native v1 reader progressively looser: two unrelated schemas
that both happen to contain ``service`` and ``titles`` are not the same format.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .chapters import Chapter
from .naming import save_name_for
from .titles import Title, TitleKind

UNSHACKLE_V2 = "unshackle-v2"
UNSHACKLE_LEGACY_TRACKS = "unshackle-legacy-tracks"
UNSHACKLE_LEGACY_SERIES = "unshackle-legacy-series"
MEDIAEXPORT = "mediaexport"

_HEX_128 = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)
_MANIFEST_SUFFIXES = {
    ".mpd": "DASH",
    ".m3u": "HLS",
    ".m3u8": "HLS",
    ".ism": "ISM",
}
_DIRECT_SUFFIXES = {
    ".aac",
    ".ac3",
    ".ass",
    ".ec3",
    ".eac3",
    ".m4a",
    ".m4s",
    ".mka",
    ".mkv",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".srt",
    ".ssa",
    ".ttml",
    ".vtt",
    ".webm",
}
_EPISODE_MARKER = re.compile(r"(?i)(?<![A-Za-z0-9])S(?P<season>\d{1,3})E(?P<episode>\d{1,4})(?!\d)")
_YEAR_MARKER = re.compile(r"(?:\(|\b)(?P<year>(?:19|20)\d{2})(?:\)|\b)")


def load(raw: Mapping[str, Any]):
    """Recognize and convert one supported non-UniDL export document."""
    from .exports import ExportError

    if _looks_like_mediaexport(raw):
        return _load_mediaexport(raw)
    if _looks_like_unshackle_v2(raw):
        return _load_unshackle_v2(raw)
    if _looks_like_unshackle_legacy_series(raw):
        return _load_unshackle_legacy_series(raw)
    if _looks_like_unshackle_legacy_tracks(raw):
        return _load_unshackle_legacy_tracks(raw)
    version = raw.get("version") if isinstance(raw, Mapping) else None
    raise ExportError(
        "not a supported export"
        " (third-party readers: mediaexport, Unshackle v2 and legacy track/series exports; "
        f"document version: {version!r})"
    )


def _looks_like_mediaexport(raw: Mapping[str, Any]) -> bool:
    return raw.get("kind") == "mediaexport"


def _load_mediaexport(raw: Mapping[str, Any]):
    """Read the shared mediaexport v1 shape into the inert UniDL model.

    This adapter deliberately consumes only settled shared fields. In particular,
    it does not interpret HLS AES URI records or frozen ``segments`` inventories
    until their cross-tool download semantics are finalized.
    """
    from .exports import Document, ExportError

    version = raw.get("version")
    if type(version) is not int or version < 1:
        raise ExportError(f"mediaexport version must be an integer, got {version!r}")
    if version > 1:
        raise ExportError(f"mediaexport version {version} is newer than this build (1)")
    service = raw.get("service")
    if not isinstance(service, Mapping) or not str(service.get("tag") or "").strip():
        raise ExportError("mediaexport does not say which service it came from")
    titles = raw.get("titles")
    if not isinstance(titles, list) or not titles:
        raise ExportError("mediaexport has no titles in it")

    entries = []
    seen_ids: set[str] = set()
    for position, title in enumerate(titles, start=1):
        if not isinstance(title, Mapping):
            raise ExportError(f"mediaexport title {position} is not an object")
        entry = _mediaexport_entry(title, position=position)
        if entry.title is not None and entry.title.id in seen_ids:
            raise ExportError(f"mediaexport contains duplicate title id {entry.title.id!r}")
        seen_ids.add(entry.title.id if entry.title is not None else entry.save_name)
        entries.append(entry)
    return Document(
        service=str(service.get("tag")).strip(),
        service_name=str(service.get("name") or service.get("tag")).strip(),
        app=str((raw.get("generator") or {}).get("app") or "mediaexport"),
        created=str(raw.get("created") or ""),
        entries=entries,
        source_format=MEDIAEXPORT,
    )


def _mediaexport_entry(title: Mapping[str, Any], *, position: int):
    from .exports import Entry, ExportError

    title_id = str(title.get("id") or f"title-{position}").strip()
    critical = title.get("crit")
    if critical is not None:
        if not isinstance(critical, list) or not critical or any(not isinstance(item, str) or not item for item in critical):
            raise ExportError(f"mediaexport title {title_id!r} has invalid crit")
        if len(set(critical)) != len(critical):
            raise ExportError(f"mediaexport title {title_id!r} repeats a crit field")
        # UniDL currently implements only the settled base fields. A critical
        # extension is a hard refusal; never fall back to a normal manifest and
        # risk silently downloading the wrong source.
        unsupported = list(critical)
        if unsupported:
            raise ExportError(
                f"mediaexport title {title_id!r} requires unsupported field {unsupported[0]!r}"
            )
    kind = str(title.get("kind") or "movie").strip().casefold()
    if kind not in {"movie", "episode", "song", "clip"}:
        kind = "movie"
    title_kind = {"song": TitleKind.TRACK, "clip": TitleKind.CLIP}.get(kind)
    if title_kind is None:
        title_kind = TitleKind(kind)
    name = str(title.get("title") or "").strip()
    if not name:
        raise ExportError(f"mediaexport title {title_id!r} has no title")

    manifests: list[Mapping[str, Any]] = []
    raw_manifests = title.get("manifests") or []
    if not isinstance(raw_manifests, list):
        raise ExportError(f"mediaexport title {title_id!r} manifests must be a list")
    for manifest in raw_manifests:
        if not isinstance(manifest, Mapping) or not str(manifest.get("url") or "").strip():
            continue
        manifests.append(manifest)

    tracks = title.get("tracks") or []
    if not isinstance(tracks, list):
        raise ExportError(f"mediaexport title {title_id!r} tracks must be a list")
    # A row with a URL is executable only when its type is a side-load. Rows
    # without a URL remain informational and are intentionally not fabricated
    # into a source URL.
    track_docs: list[dict[str, Any]] = []
    for index, row in enumerate(tracks, start=1):
        if not isinstance(row, Mapping):
            continue
        url = _url(row.get("url"))
        if not url:
            continue
        media_type = str(row.get("type") or "").strip().casefold()
        if media_type not in {"video", "audio", "subtitle"}:
            continue
        document = dict(row)
        document.update(
            id=str(row.get("id") or f"track-{index}"),
            type=media_type,
            url=url,
            descriptor=_manifest_type(row.get("descriptor"), url),
        )
        track_docs.append(document)

    primary = next(
        (m for m in manifests if str(m.get("role") or "").casefold() == "primary"),
        next((m for m in manifests if str(m.get("role") or "").casefold() != "extra"), None),
    )
    manifest_urls = [str(m["url"]).strip() for m in manifests]
    if primary is not None:
        primary_url = str(primary["url"]).strip()
        alternates = tuple(url for url in manifest_urls if url != primary_url)
    else:
        primary_url = ""
        alternates = ()

    # If there is no manifest, direct side-load rows are still a valid title.
    json_manifest = _json_manifest(
        Title(
            id=title_id,
            kind=title_kind,
            name=name,
            service="third-party",
        ),
        track_docs,
    ) if track_docs else None
    if not primary_url and json_manifest is None:
        raise ExportError(f"mediaexport title {title_id!r} has no downloadable manifest or track URL")

    # A side-loaded row needs the JSON variant alongside the primary manifest.
    # Let Engine merge the inert JSON source with every authorized manifest;
    # ordinary manifest-only exports keep their fast primary path.
    json_plus_manifests = bool(json_manifest and primary_url)
    effective_primary_url = "" if json_plus_manifests else primary_url
    effective_alternates = tuple(manifest_urls) if json_plus_manifests else alternates

    key_pairs = _mediaexport_keys(title.get("keys"), title_id=title_id)
    drm_system, pssh, wrm_header, protected = _mediaexport_drm(title.get("drm"), title_id=title_id)
    if protected and not key_pairs:
        note = "protected tracks contain no exported content key"
    else:
        note = ""
    note_parts = ["third-party export · generic downloader · no service login or licence"]
    if note:
        note_parts.append(note)
    return Entry(
        save_name=str(title.get("release_name") or name),
        title=Title(
            id=title_id,
            kind=title_kind,
            name=str(title.get("series") or name) if kind == "episode" else name,
            episode_name=name if kind == "episode" else None,
            season=_optional_int(title.get("season")),
            episode=_optional_int(title.get("episode")),
            year=_optional_text(title.get("year")),
            language=_optional_text(title.get("language")),
            service="third-party",
        ),
        manifest_url=effective_primary_url,
        alternate_manifest_urls=effective_alternates,
        merge_manifests=len(manifest_urls) > 1,
        json_manifest=json_manifest,
        headers=_mediaexport_headers(primary),
        note=" · ".join(note_parts),
        drm_system=drm_system,
        pssh=pssh,
        wrm_header=wrm_header,
        keys=key_pairs,
        chapters=_mediaexport_chapters(title.get("chapters"), title_id=title_id),
        summary=f"mediaexport · {len(tracks)} exported track(s) · {len(key_pairs)} content key(s)",
    )


def _mediaexport_headers(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    raw = manifest.get("headers") if manifest else {}
    if not isinstance(raw, Mapping):
        return {}
    forbidden = {"cookie", "authorization"}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if value is not None and str(key).casefold() not in forbidden
    }


def _mediaexport_keys(value: Any, *, title_id: str) -> list[str]:
    from .exports import ExportError

    if value in (None, {}):
        return []
    if not isinstance(value, Mapping):
        raise ExportError(f"mediaexport title {title_id!r} keys must be an object")
    found: dict[str, str] = {}
    for raw_kid, raw_key in value.items():
        if not raw_kid or not raw_key:
            continue
        kid = _kid(raw_kid)
        key = str(raw_key).strip().casefold().replace("-", "")
        if not _HEX_128.fullmatch(key):
            raise ExportError(f"mediaexport title {title_id!r} has an invalid content key")
        previous = found.get(kid)
        if previous is not None and previous != key:
            raise ExportError(f"mediaexport title {title_id!r} contains conflicting keys for KID {kid}")
        found.setdefault(kid, key)
    return [f"{kid}:{key}" for kid, key in found.items()]


def _mediaexport_drm(value: Any, *, title_id: str) -> tuple[str, str, str, bool]:
    from .exports import ExportError

    if value in (None, []):
        return "", "", "", False
    if not isinstance(value, list):
        raise ExportError(f"mediaexport title {title_id!r} drm must be a list")
    systems: list[str] = []
    pssh = ""
    wrm_header = ""
    for item in value:
        if not isinstance(item, Mapping):
            continue
        system = str(item.get("system") or "").strip().casefold()
        if system and system not in systems:
            systems.append(system)
        if not pssh and item.get("pssh"):
            pssh = str(item["pssh"])
        if not wrm_header and item.get("wrm_header"):
            wrm_header = str(item["wrm_header"])
    return (systems[0] if systems else "", pssh, wrm_header, bool(value))


def _mediaexport_chapters(value: Any, *, title_id: str) -> list[Chapter]:
    from .exports import ExportError

    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ExportError(f"mediaexport title {title_id!r} chapters must be a list")
    chapters: list[Chapter] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise ExportError(f"mediaexport title {title_id!r} chapter {index} is invalid")
        try:
            chapters.append(Chapter.from_document(item))
        except (TypeError, ValueError) as exc:
            raise ExportError(f"mediaexport title {title_id!r} chapter {index} is invalid ({exc})") from exc
    return chapters


def _looks_like_unshackle_v2(raw: Mapping[str, Any]) -> bool:
    titles = raw.get("titles")
    # The map-shaped title collection is Unshackle's distinguishing v2 shape.
    # Require its entries to look like exported title records as well, so a
    # future downloader's unrelated {version, service, titles} document is not
    # silently interpreted as this format.
    has_title_record = bool(
        isinstance(titles, Mapping)
        and any(
            isinstance(value, Mapping)
            and ("meta" in value or "tracks" in value)
            for value in titles.values()
        )
    )
    return (
        raw.get("version") == 2
        and bool(str(raw.get("service") or "").strip())
        and has_title_record
    )


def _looks_like_unshackle_legacy_tracks(raw: Mapping[str, Any]) -> bool:
    """Recognize the old ``title -> human label -> track`` export shape."""
    if not raw or any(key in raw for key in ("kind", "version", "titles", "seasons")):
        return False
    track_documents: list[Mapping[str, Any]] = []
    for title in raw.values():
        if not isinstance(title, Mapping) or not title:
            return False
        if not all(isinstance(track, Mapping) for track in title.values()):
            return False
        track_documents.extend(track for track in title.values() if isinstance(track, Mapping))
    return bool(track_documents) and all(
        bool(_url(track.get("url") or track.get("manifest_url")))
        for track in track_documents
    )


def _looks_like_unshackle_legacy_series(raw: Mapping[str, Any]) -> bool:
    """Recognize Unshackle's older series/season/episode collection."""
    seasons = raw.get("seasons")
    if not (
        isinstance(seasons, Mapping)
        and seasons
        and bool(str(raw.get("service") or "").strip())
        and bool(str(raw.get("name") or "").strip())
    ):
        return False
    for season in seasons.values():
        if not isinstance(season, Mapping):
            return False
        episodes = season.get("episodes")
        if not isinstance(episodes, Mapping):
            return False
        for episode in episodes.values():
            if not isinstance(episode, Mapping) or not isinstance(episode.get("tracks"), Mapping):
                return False
    return True


def _load_unshackle_v2(raw: Mapping[str, Any]):
    from .exports import Document, ExportError

    service = str(raw.get("service") or "").strip()
    titles = raw.get("titles")
    if not isinstance(titles, Mapping) or not titles:
        raise ExportError("Unshackle v2 export has no titles")

    region = str(raw.get("region") or "").strip().upper()
    entries = []
    for fallback_id, value in titles.items():
        if not isinstance(value, Mapping):
            raise ExportError(
                f"Unshackle title {fallback_id!r} is not an object"
            )
        entries.append(
            _unshackle_entry(
                service,
                str(fallback_id),
                value,
                region=region,
            )
        )

    return Document(
        service=service,
        service_name=f"{service} · Unshackle",
        app="Unshackle",
        entries=entries,
        source_format=UNSHACKLE_V2,
    )


def _load_unshackle_legacy_tracks(raw: Mapping[str, Any]):
    """Convert the pre-v2 map whose track names carried most metadata."""
    from .exports import Document, ExportError

    entries = []
    for position, (title_label, value) in enumerate(raw.items(), start=1):
        if not isinstance(value, Mapping):
            raise ExportError(f"legacy title {title_label!r} is not an object")
        title = _legacy_label_title(str(title_label), position=position)
        tracks = [
            _legacy_label_track(str(label), track, position=index)
            for index, (label, track) in enumerate(value.items(), start=1)
            if isinstance(track, Mapping)
        ]
        if not tracks:
            raise ExportError(f"legacy title {title_label!r} has no tracks")
        keys = _key_pool(tracks, title_id=title.id)
        entries.append(
            _legacy_entry(
                title,
                tracks,
                keys=keys,
                source_label="Unshackle legacy track export",
            )
        )

    if not entries:
        raise ExportError("Unshackle legacy track export has no titles")
    return Document(
        service="third-party",
        service_name="Unshackle · legacy track export",
        app="Unshackle",
        entries=entries,
        source_format=UNSHACKLE_LEGACY_TRACKS,
    )


def _load_unshackle_legacy_series(raw: Mapping[str, Any]):
    """Convert the older series catalog with tracks nested under episodes."""
    from .exports import Document, ExportError

    service = str(raw.get("service") or "third-party").strip()
    series_name = str(raw.get("name") or raw.get("id") or "Untitled series").strip()
    seasons = raw.get("seasons")
    if not isinstance(seasons, Mapping):
        raise ExportError("Unshackle legacy series export has invalid seasons")

    entries = []
    for season_label, season in seasons.items():
        if not isinstance(season, Mapping):
            raise ExportError(f"Unshackle season {season_label!r} is not an object")
        episodes = season.get("episodes")
        if not isinstance(episodes, Mapping):
            raise ExportError(f"Unshackle season {season_label!r} has invalid episodes")
        for episode_label, episode in episodes.items():
            if not isinstance(episode, Mapping):
                raise ExportError(
                    f"Unshackle episode {season_label!r}/{episode_label!r} is not an object"
                )
            title_id = str(
                episode.get("id")
                or _stable_id("episode", series_name, season_label, episode_label)
            )
            title = Title(
                id=title_id,
                kind=TitleKind.EPISODE,
                name=series_name,
                year=_optional_text(raw.get("year")),
                season=_optional_int(season_label),
                episode=_optional_int(episode_label),
                episode_name=_optional_text(episode.get("episode_name")),
                language=_optional_text(raw.get("language")),
                service=service.lower(),
            )
            track_root = episode.get("tracks")
            if not isinstance(track_root, Mapping):
                raise ExportError(f"Unshackle episode {title_id!r} has invalid tracks")
            tracks = _legacy_series_tracks(track_root, title_id=title_id)
            exported_keys = _key_list(
                track_root.get("keys"),
                title_id=title_id,
            )
            keys = _combine_key_pairs(
                _key_pool(tracks, title_id=title_id),
                exported_keys,
                title_id=title_id,
            )
            chapters = _chapters(track_root.get("chapters"), title_id=title_id)
            entries.append(
                _legacy_entry(
                    title,
                    tracks,
                    keys=keys,
                    chapters=chapters,
                    source_label="Unshackle legacy series export",
                )
            )

    if not entries:
        raise ExportError("Unshackle legacy series export has no episodes")
    return Document(
        service=service,
        service_name=f"{service} · Unshackle legacy",
        app="Unshackle",
        entries=entries,
        source_format=UNSHACKLE_LEGACY_SERIES,
    )


def _legacy_entry(
    title: Title,
    tracks: list[Mapping[str, Any]],
    *,
    keys: list[str],
    source_label: str,
    chapters: list[Chapter] | None = None,
):
    from .exports import Entry, ExportError

    manifest_url, alternates, json_manifest, source_count = _delivery_sources(
        title,
        tracks,
    )
    if not manifest_url and json_manifest is None:
        raise ExportError(f"{source_label} title {title.id!r} has no downloadable source")
    drm_system, pssh, wrm_header, protected = _drm_summary(tracks)
    protected = protected or any(
        bool(track.get("keys")) or _flag(track.get("encrypted"))
        for track in tracks
    )
    note_parts = ["third-party export · generic downloader · no service login or licence"]
    if source_count > 1:
        note_parts.append(f"{source_count} exported manifest sources merged")
    if protected and not keys:
        note_parts.append("protected tracks contain no exported content key")
    return Entry(
        save_name=save_name_for(title) or title.id,
        title=title,
        manifest_url=manifest_url,
        alternate_manifest_urls=alternates,
        merge_manifests=source_count > 1,
        json_manifest=json_manifest,
        note=" · ".join(note_parts),
        drm_system=drm_system,
        pssh=pssh,
        wrm_header=wrm_header,
        keys=keys,
        chapters=list(chapters or []),
        summary=(
            f"{source_label} · {len(tracks)} exported track(s) · "
            f"{len(keys)} content key(s)"
        ),
    )


def _legacy_label_title(label: str, *, position: int) -> Title:
    cleaned = label.strip() or f"Title {position}"
    year_match = _YEAR_MARKER.search(cleaned)
    year = year_match.group("year") if year_match else None
    episode_match = _EPISODE_MARKER.search(cleaned)
    title_id = _stable_id("title", cleaned)
    if episode_match:
        before = cleaned[: episode_match.start()]
        if year_match and year_match.start() < episode_match.start():
            before = before[: year_match.start()]
        series = before.strip(" ._-()") or cleaned
        episode_name = cleaned[episode_match.end() :].strip(" ._-") or None
        return Title(
            id=title_id,
            kind=TitleKind.EPISODE,
            name=series,
            year=year,
            season=int(episode_match.group("season")),
            episode=int(episode_match.group("episode")),
            episode_name=episode_name,
            service="third-party",
        )
    name = cleaned[: year_match.start()].strip(" ._-()") if year_match else cleaned
    return Title(
        id=title_id,
        kind=TitleKind.MOVIE,
        name=name or cleaned,
        year=year,
        service="third-party",
    )


def _legacy_label_track(
    label: str,
    track: Mapping[str, Any],
    *,
    position: int,
) -> Mapping[str, Any]:
    from .exports import ExportError

    url = _url(track.get("url") or track.get("manifest_url"))
    if not url:
        raise ExportError(f"legacy track {position} has no URL")
    media_type = _legacy_media_type(track.get("type"), label)
    if not media_type:
        raise ExportError(f"legacy track {position} has no recognizable media type")
    result = dict(track)
    result.update(
        id=str(track.get("id") or _stable_id(media_type, label, url)),
        type=media_type,
        url=url,
    )
    raw_urls = track.get("url") or track.get("urls")
    if isinstance(raw_urls, (list, tuple)):
        all_urls = [candidate for candidate in (_url(item) for item in raw_urls) if candidate]
        if len(all_urls) > 1:
            result["all_urls"] = all_urls
    fields = [part.strip() for part in label.split("|")]
    result.setdefault("codec", _legacy_codec(fields, media_type))
    result.setdefault("language", _legacy_language(fields))
    if media_type == "video":
        result.setdefault("resolution", _legacy_resolution(label))
        result.setdefault("fps", _legacy_fps(label))
        result.setdefault("range", _legacy_video_range(label))
    elif media_type == "audio":
        result.setdefault("channels", _legacy_channels(label))
    else:
        result.setdefault("forced", "[forced]" in label.casefold())
        result.setdefault("sdh", "[sdh]" in label.casefold())
        result.setdefault("cc", "[cc]" in label.casefold())
    return {key: value for key, value in result.items() if value not in (None, "")}


def _legacy_series_tracks(
    root: Mapping[str, Any],
    *,
    title_id: str,
) -> list[Mapping[str, Any]]:
    from .exports import ExportError

    tracks: list[Mapping[str, Any]] = []
    for group, media_type in (
        ("videos", "video"),
        ("audios", "audio"),
        ("subtitles", "subtitle"),
    ):
        collection = root.get(group) or {}
        if not isinstance(collection, Mapping):
            raise ExportError(f"Unshackle episode {title_id!r} {group} must be an object")
        for label, value in collection.items():
            items = value if isinstance(value, list) else [value]
            for position, item in enumerate(items, start=1):
                if isinstance(item, str):
                    document: dict[str, Any] = {"url": item}
                elif isinstance(item, Mapping):
                    document = dict(item)
                else:
                    raise ExportError(
                        f"Unshackle episode {title_id!r} {group} track {position} is invalid"
                    )
                # The legacy series schema stores both a resolved media-playlist
                # URL and the master that produced it. Keep the resolved child;
                # importing must not rebuild a different ladder from the master.
                url = _url(document.get("url") or document.get("manifest_url"))
                if not url:
                    raise ExportError(
                        f"Unshackle episode {title_id!r} {group} track {position} has no URL"
                    )
                document.update(
                    id=str(
                        document.get("id")
                        or _stable_id(media_type, title_id, label, position, url)
                    ),
                    type=media_type,
                    url=url,
                    descriptor=_manifest_type(document.get("descriptor"), url),
                )
                document.pop("manifest_url", None)
                language, display_name = _legacy_language_label(str(label))
                if not document.get("language"):
                    document["language"] = language
                if not document.get("name"):
                    document["name"] = display_name
                if media_type == "video":
                    fields = [part.strip() for part in str(label).split("|")]
                    if not document.get("codec"):
                        document["codec"] = _legacy_codec(fields, "video")
                    if not document.get("resolution"):
                        document["resolution"] = document.get("quality") or _legacy_resolution(str(label))
                    if "fps" not in document:
                        document["fps"] = document.get("framerate") or _legacy_fps(str(label))
                    if not document.get("range"):
                        document["range"] = _legacy_video_range(str(label))
                elif media_type == "audio":
                    fields = [part.strip() for part in str(label).split("|")]
                    if not document.get("codec"):
                        document["codec"] = _legacy_codec(fields, "audio")
                    if not document.get("channels"):
                        document["channels"] = _legacy_channels(str(label))
                elif media_type == "subtitle":
                    fields = [part.strip() for part in str(label).split("|")]
                    if not document.get("codec"):
                        document["codec"] = _legacy_codec(fields, "subtitle")
                    lowered = str(label).casefold()
                    document.setdefault("forced", "forced" in lowered)
                    document.setdefault("sdh", "sdh" in lowered)
                    document.setdefault("cc", "closed caption" in lowered)
                tracks.append(
                    {key: value for key, value in document.items() if value not in (None, "")}
                )
    if not tracks:
        raise ExportError(f"Unshackle episode {title_id!r} has no downloadable tracks")
    return tracks


def _stable_id(*values: Any) -> str:
    material = "\0".join(str(value) for value in values).encode("utf-8", errors="replace")
    return sha256(material).hexdigest()[:16]


def _legacy_media_type(value: Any, label: str) -> str:
    explicit = str(value or "").strip().casefold()
    aliases = {
        "vid": "video",
        "video": "video",
        "aud": "audio",
        "audio": "audio",
        "sub": "subtitle",
        "subtitle": "subtitle",
        "text": "subtitle",
    }
    if explicit in aliases:
        return aliases[explicit]
    prefix = re.match(r"(?i)^\s*(VID|VIDEO|AUD|AUDIO|SUB|SUBTITLE|TEXT)\b", label)
    return aliases.get(prefix.group(1).casefold(), "") if prefix else ""


def _legacy_codec(fields: list[str], media_type: str) -> str | None:
    if not fields:
        return None
    # Full labels start with VID/AUD/SUB; compact series group labels often
    # start directly with the codec (for example ``AAC | 2.0 | en``).
    offset = 1 if fields[0].casefold() in {"vid", "video", "aud", "audio", "sub", "subtitle", "text"} else 0
    if media_type == "video":
        pattern = r"(?i)(H\.?26[45]|HEVC|AVC|AV1|VP9|VP8|VVC)"
    elif media_type == "audio":
        pattern = r"(?i)(E-?AC-?3|EC-?3|AC-?3|AAC|DD\+?|ATMOS|OPUS|FLAC)"
    else:
        pattern = None
    candidates = [field.strip(" []") for field in fields[offset:] if field.strip(" []")]
    if pattern:
        for candidate in candidates:
            match = re.search(pattern, candidate)
            if match:
                return match.group(1)
    return candidates[0] if candidates else None


def _legacy_language(fields: list[str]) -> str | None:
    for field in fields[2:]:
        candidate = field.strip(" []")
        if re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2,8})?", candidate):
            return candidate
    return None


def _legacy_language_label(label: str) -> tuple[str | None, str | None]:
    language, separator, name = label.partition(" - ")
    candidate = language.strip()
    if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,8})?", candidate):
        return candidate, name.strip() or None
    return None, label.strip() or None


def _legacy_resolution(label: str) -> str | None:
    match = re.search(r"\b\d{2,5}x\d{2,5}\b", label, re.IGNORECASE)
    if match:
        return match.group(0).lower()
    match = re.search(r"\b\d{3,4}p\b", label, re.IGNORECASE)
    return match.group(0).lower() if match else None


def _legacy_fps(label: str) -> str | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*FPS\b", label, re.IGNORECASE)
    return match.group(1) if match else None


def _legacy_channels(label: str) -> str | None:
    match = re.search(r"(?:^|\|)\s*(\d+(?:\.\d+)?)\s*(?=\||$)", label)
    return match.group(1) if match else None


def _legacy_video_range(label: str) -> str | None:
    upper = label.upper()
    ranges = []
    if any(token in upper for token in ("DOLBY VISION", "DOVI", " DV ")):
        ranges.append("DV")
    if "HDR10+" in upper or "HDR10P" in upper:
        ranges.append("HDR10+")
    elif "HDR10" in upper or " HDR " in upper:
        ranges.append("HDR10")
    if ranges:
        return "+".join(ranges)
    return "SDR" if "SDR" in upper else None


def _unshackle_entry(
    service: str,
    fallback_id: str,
    entry: Mapping[str, Any],
    *,
    region: str,
):
    from .exports import Entry, ExportError

    meta = entry.get("meta") or {}
    if not isinstance(meta, Mapping):
        raise ExportError(f"Unshackle title {fallback_id!r} has invalid metadata")
    title = _unshackle_title(service, fallback_id, meta)
    tracks = _track_documents(entry.get("tracks"), title_id=fallback_id)
    keys = _key_pool(tracks, title_id=fallback_id)
    chapters = _chapters(entry.get("chapters"), title_id=fallback_id)
    manifest_url, alternates, json_manifest, source_count = _delivery_sources(
        title,
        tracks,
        top_url=entry.get("manifest_url"),
        top_type=entry.get("manifest_type"),
    )

    if not manifest_url and json_manifest is None:
        raise ExportError(
            f"Unshackle title {fallback_id!r} has no downloadable manifest or track URL"
        )

    drm_system, pssh, wrm_header, protected = _drm_summary(tracks)
    note_parts = ["third-party export · generic downloader · no service login or licence"]
    if region:
        note_parts.append(f"source region {region}; choose a UniDL proxy separately if needed")
    if source_count > 1:
        note_parts.append(f"{source_count} exported manifest sources merged")
    if protected and not keys:
        note_parts.append("protected tracks contain no exported content key")

    return Entry(
        save_name=save_name_for(title) or fallback_id,
        title=title,
        manifest_url=manifest_url,
        alternate_manifest_urls=alternates,
        merge_manifests=source_count > 1,
        json_manifest=json_manifest,
        is_live=bool(entry.get("is_live")),
        note=" · ".join(note_parts),
        drm_system=drm_system,
        pssh=pssh,
        wrm_header=wrm_header,
        keys=keys,
        chapters=chapters,
        summary=(
            f"Unshackle v2 · {len(tracks)} exported track(s) · "
            f"{len(keys)} content key(s)"
        ),
    )


def _delivery_sources(
    title: Title,
    tracks: list[Mapping[str, Any]],
    *,
    top_url: Any = None,
    top_type: Any = None,
) -> tuple[str, tuple[str, ...], dict[str, Any] | None, int]:
    """Split exported sources into a typed direct/HLS source and manifests."""
    hls_tracks = [
        track
        for track in tracks
        if _manifest_type(track.get("descriptor"), track.get("url")) == "HLS"
    ]
    direct_tracks = [
        track
        for track in tracks
        if _manifest_type(track.get("descriptor"), track.get("url")) == "URL"
    ]
    # HLS exports deliberately retain media-playlist URLs per track. Reusing
    # those avoids re-contacting a master playlist that may mint different or
    # already-expired child tokens. Direct side-loaded tracks can share the same
    # typed JSON source.
    json_tracks: list[Mapping[str, Any]] = [*hls_tracks, *direct_tracks]
    manifest_urls: list[str] = []
    top = _url(top_url)
    declared_type = _manifest_type(top_type, top)
    # A resolved HLS ladder uses the exported child URLs above and does not need
    # its master again. A master-only export still needs that top URL. DASH/ISM
    # top-level manifests are always real sources to merge.
    if top and (declared_type in {"DASH", "ISM"} or not hls_tracks):
        manifest_urls.append(top)
    for track in tracks:
        track_type = _manifest_type(track.get("descriptor"), track.get("url"))
        if track_type not in {"DASH", "ISM"}:
            continue
        url = _url(track.get("url"))
        if url and url not in manifest_urls:
            manifest_urls.append(url)

    json_manifest = _json_manifest(title, json_tracks) if json_tracks else None
    if json_manifest is not None:
        # The JSON source is the primary variant. DASH/ISM URLs, if any, are
        # additional generic variants merged by the inert import service.
        return "", tuple(manifest_urls), json_manifest, 1 + len(manifest_urls)
    manifest_url = manifest_urls[0] if manifest_urls else ""
    return manifest_url, tuple(manifest_urls[1:]), None, len(manifest_urls)


def _unshackle_title(
    service: str,
    fallback_id: str,
    meta: Mapping[str, Any],
) -> Title:
    title_id = str(meta.get("id") or fallback_id)
    kind_text = str(meta.get("type") or "movie").strip().casefold()
    if kind_text == "episode":
        series = str(
            meta.get("series_title")
            or meta.get("series")
            or meta.get("title")
            or meta.get("name")
            or fallback_id
        ).strip()
        episode_name = str(meta.get("name") or "").strip() or None
        if episode_name == series:
            episode_name = None
        return Title(
            id=title_id,
            kind=TitleKind.EPISODE,
            name=series,
            year=_optional_text(meta.get("year")),
            season=_optional_int(meta.get("season")),
            episode=_optional_int(meta.get("number") or meta.get("episode")),
            episode_name=episode_name,
            language=_optional_text(meta.get("language")),
            service=service.lower(),
        )
    return Title(
        id=title_id,
        kind=TitleKind.MOVIE,
        name=str(meta.get("name") or meta.get("title") or fallback_id).strip(),
        year=_optional_text(meta.get("year")),
        language=_optional_text(meta.get("language")),
        service=service.lower(),
    )


def _track_documents(value: Any, *, title_id: str) -> list[Mapping[str, Any]]:
    from .exports import ExportError

    if value in (None, {}):
        return []
    if not isinstance(value, Mapping):
        raise ExportError(f"Unshackle title {title_id!r} tracks must be an object")
    tracks: list[Mapping[str, Any]] = []
    for track_id, track in value.items():
        if not isinstance(track, Mapping):
            raise ExportError(
                f"Unshackle title {title_id!r} track {track_id!r} is not an object"
            )
        if "id" not in track:
            track = {**track, "id": str(track_id)}
        tracks.append(track)
    return tracks


def _key_pool(
    tracks: list[Mapping[str, Any]],
    *,
    title_id: str,
) -> list[str]:
    from .exports import ExportError

    found: dict[str, str] = {}
    for track in tracks:
        track_id = str(track.get("id") or "unknown")
        values = track.get("keys") or {}
        if not isinstance(values, Mapping):
            raise ExportError(
                f"Unshackle title {title_id!r} track {track_id!r} keys must be an object"
            )
        for raw_kid, raw_key in values.items():
            kid = _kid(raw_kid)
            key = str(raw_key or "").strip().lower().replace("-", "")
            if not _HEX_128.fullmatch(key):
                raise ExportError(
                    f"Unshackle title {title_id!r} track {track_id!r} has an invalid content key"
                )
            previous = found.get(kid)
            if previous is not None and previous != key:
                raise ExportError(
                    f"Unshackle title {title_id!r} contains conflicting keys for KID {kid}"
                )
            found.setdefault(kid, key)
    return [f"{kid}:{key}" for kid, key in found.items()]


def _key_list(value: Any, *, title_id: str) -> list[str]:
    """Validate a legacy top-level list of already-resolved KID:key pairs."""
    from .exports import ExportError

    if value in (None, [], {}):
        return []
    if isinstance(value, Mapping):
        values = [f"{kid}:{key}" for kid, key in value.items()]
    elif isinstance(value, list):
        values = value
    else:
        raise ExportError(f"Unshackle title {title_id!r} keys must be a list or object")
    pairs: list[str] = []
    for position, raw_pair in enumerate(values, start=1):
        raw_kid, separator, raw_key = str(raw_pair or "").partition(":")
        if not separator:
            raise ExportError(
                f"Unshackle title {title_id!r} key {position} is not KID:key"
            )
        kid = _kid(raw_kid)
        key = raw_key.strip().lower().replace("-", "")
        if not _HEX_128.fullmatch(key):
            raise ExportError(
                f"Unshackle title {title_id!r} key {position} has an invalid content key"
            )
        pairs.append(f"{kid}:{key}")
    return _combine_key_pairs(pairs, title_id=title_id)


def _combine_key_pairs(*groups: list[str], title_id: str) -> list[str]:
    from .exports import ExportError

    found: dict[str, str] = {}
    for pair in (pair for group in groups for pair in group):
        kid, separator, key = str(pair).partition(":")
        if not separator:
            raise ExportError(f"Unshackle title {title_id!r} contains an invalid key pair")
        previous = found.get(kid)
        if previous is not None and previous != key:
            raise ExportError(
                f"Unshackle title {title_id!r} contains conflicting keys for KID {kid}"
            )
        found.setdefault(kid, key)
    return [f"{kid}:{key}" for kid, key in found.items()]


def _kid(value: Any) -> str:
    from .exports import ExportError

    text = str(value or "").strip().strip("{}")
    try:
        return UUID(text).hex
    except ValueError:
        compact = text.lower().replace("-", "")
        if _HEX_128.fullmatch(compact):
            return compact
    raise ExportError(f"Unshackle export contains an invalid KID {text!r}")


def _drm_summary(
    tracks: list[Mapping[str, Any]],
) -> tuple[str, str, str, bool]:
    systems: list[str] = []
    pssh = ""
    wrm_header = ""
    protected = False
    for track in tracks:
        values = track.get("drm") or []
        if isinstance(values, Mapping):
            values = [values]
        if not isinstance(values, list):
            continue
        for drm in values:
            if not isinstance(drm, Mapping):
                continue
            protected = True
            raw_system = str(drm.get("system") or "widevine").strip().casefold()
            system = {
                "wv": "widevine",
                "widevine": "widevine",
                "pr": "playready",
                "playready": "playready",
                "clearkeycenc": "clearkeycenc",
                "clearkey": "clearkeycenc",
            }.get(raw_system, raw_system)
            if system and system not in systems:
                systems.append(system)
            init_data = str(
                drm.get("pssh_b64") or drm.get("pssh") or ""
            ).strip()
            if system == "playready":
                header = str(drm.get("wrm_header") or "").strip()
                if not header and init_data:
                    # Unshackle normally exports the PlayReady object as base64
                    # PSSH.  Convert it locally when pyplayready is available;
                    # this is parsing only and never creates a challenge.  A
                    # key-complete import does not need the hint, so absence of
                    # the optional library remains harmless.
                    try:
                        from .playready import wrm_headers_from_pssh

                        header = next(iter(wrm_headers_from_pssh(init_data)), "")
                    except Exception:  # noqa: BLE001 - optional parser only
                        header = ""
                if header and not wrm_header:
                    wrm_header = header
            elif init_data and not pssh:
                pssh = init_data
    return (systems[0] if systems else "", pssh, wrm_header, protected)


def _json_manifest(
    title: Title,
    tracks: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    buckets: dict[str, list[dict[str, Any]]] = {
        "video_tracks": [],
        "audio_tracks": [],
        "subtitle_tracks": [],
    }
    for track in tracks:
        media_type = str(track.get("type") or "").strip().casefold()
        bucket = {
            "video": "video_tracks",
            "audio": "audio_tracks",
            "subtitle": "subtitle_tracks",
            "text": "subtitle_tracks",
        }.get(media_type)
        if bucket is None:
            continue
        converted = _json_track(track, media_type=media_type)
        if converted:
            buckets[bucket].append(converted)
    if not any(buckets.values()):
        return None
    document = {
        "id": title.id,
        "type": title.kind.value,
        "name": title.name,
        "season": title.season,
        "episode": title.episode,
        "episode_name": title.episode_name,
        "original_language": title.language,
        **buckets,
    }
    # Third-party exports already carry each HLS rendition's metadata. The
    # generic parser uses this private marker to defer child-playlist requests
    # until the user selects a track; normal service JSON manifests are unchanged.
    if any(track.get("manifest_url") for bucket in buckets.values() for track in bucket):
        document["_unidl_lazy_hls"] = True
    return document


def _json_track(
    track: Mapping[str, Any],
    *,
    media_type: str,
) -> dict[str, Any] | None:
    raw_url = track.get("url")
    url = _url(raw_url)
    if not url:
        return None
    manifest_type = _manifest_type(track.get("descriptor"), url)
    result: dict[str, Any] = {
        "id": str(track.get("id") or ""),
        "language": _optional_text(track.get("language")),
        "name": _optional_text(track.get("name")),
        "codec": _optional_text(track.get("codec")),
        "bitrate": _optional_int(track.get("bitrate")),
    }
    if manifest_type == "HLS":
        result["manifest_url"] = url
    else:
        result["url"] = url
    # Unshackle permits a track to carry fallback URLs.  The native JSON parser
    # uses the first URL as the stable source (as its other JSON producers do),
    # while retaining the complete list in the opaque field for diagnostics and
    # future fallback policy.
    if isinstance(raw_url, (list, tuple)):
        urls = [candidate for candidate in (_url(item) for item in raw_url) if candidate]
        if len(urls) > 1:
            result["all_urls"] = urls

    keys = track.get("keys") or {}
    drm = track.get("drm") or []
    result["encrypted"] = bool(keys or drm or _flag(track.get("encrypted")))
    if isinstance(keys, Mapping) and keys:
        kids = [_kid(value) for value in keys]
        kid = kids[0]
        # ``kid`` is the public portable-JSON spelling; ``key_id`` is also
        # emitted because the native parser exposes it as ``extra["key_id"]``
        # for vault matching and segment diagnostics.
        result["kid"] = kid
        result["key_id"] = kid
        if len(kids) > 1:
            result["key_ids"] = list(dict.fromkeys(kids))
    elif isinstance(drm, list):
        for item in drm:
            if not isinstance(item, Mapping):
                continue
            kids = item.get("kids") or []
            if isinstance(kids, list) and kids:
                normalized_kids = [_kid(value) for value in kids]
                kid = normalized_kids[0]
                result["kid"] = kid
                result["key_id"] = kid
                if len(normalized_kids) > 1:
                    result["key_ids"] = list(dict.fromkeys(normalized_kids))
                break

    if media_type == "video":
        result.update(
            width=_optional_int(track.get("width")),
            height=_optional_int(track.get("height")),
            resolution=_optional_text(track.get("resolution")),
            fps=_optional_text(track.get("fps")),
            video_range=_optional_text(track.get("range")),
        )
    elif media_type == "audio":
        result.update(
            channels=_optional_text(track.get("channels")),
            descriptive=bool(track.get("descriptive")),
            joc=_optional_int(track.get("joc")),
            is_original=bool(track.get("is_original_lang")),
        )
    else:
        result.update(
            forced=bool(track.get("forced")),
            sdh=bool(track.get("sdh")),
            cc=bool(track.get("cc")),
        )
    return {key: value for key, value in result.items() if value not in (None, "")}


def _chapters(value: Any, *, title_id: str) -> list[Chapter]:
    from .exports import ExportError

    if not value:
        return []
    if not isinstance(value, list):
        raise ExportError(f"Unshackle title {title_id!r} chapters must be a list")
    chapters: list[Chapter] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise ExportError(
                f"Unshackle title {title_id!r} chapter {index} is not an object"
            )
        name = str(item.get("name") or "").strip()
        # Unshackle may include its nameless 00:00:00 baseline marker. It carries
        # no navigation information and UniDL chapters intentionally require a
        # title or kind.
        if not name:
            continue
        try:
            start_ms = _timestamp_ms(item.get("timestamp"))
        except ValueError as exc:
            raise ExportError(
                f"Unshackle title {title_id!r} chapter {index} has an invalid timestamp ({exc})"
            ) from exc
        chapters.append(Chapter(start_ms=start_ms, title=name))
    return chapters


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("timestamp is missing")
    if isinstance(value, (int, float)):
        if float(value) < 0:
            raise ValueError("timestamp cannot be negative")
        return round(float(value) * 1000)
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("expected MM:SS or HH:MM:SS")
    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[-3]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError("timestamp contains a non-number") from exc
    if min(hours, minutes, seconds) < 0 or minutes >= 60 or seconds >= 60:
        raise ValueError("timestamp is outside clock bounds")
    return round((hours * 3600 + minutes * 60 + seconds) * 1000)


def _manifest_type(value: Any, url: Any = None) -> str:
    text = str(value or "").strip().upper()
    aliases = {
        "M3U": "HLS",
        "M3U8": "HLS",
        "SMOOTH": "ISM",
        "SMOOTHSTREAMING": "ISM",
        "DIRECT": "URL",
        "": "",
    }
    text = aliases.get(text, text)
    suffix = urlsplit(_url(url)).path.casefold()
    # Some legacy exports labelled every segmented track ``DASH`` even when
    # the resolved URL was a standalone MP4. Trust the URL in that case: asking
    # the manifest parser to read an MP4 produces a misleading DASH error.
    if text in {"DASH", "ISM"} and any(suffix.endswith(ext) for ext in _DIRECT_SUFFIXES):
        return "URL"
    if text in {"DASH", "HLS", "ISM", "URL"}:
        return text
    if suffix.endswith(".ism/manifest") or suffix.endswith(".isml/manifest"):
        return "ISM"
    for ending, manifest_type in _MANIFEST_SUFFIXES.items():
        if suffix.endswith(ending):
            return manifest_type
    return "URL"


def _url(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            result = _url(item)
            if result:
                return result
        return ""
    text = str(value or "").strip()
    return text if text else ""


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    return bool(value)


__all__ = [
    "UNSHACKLE_LEGACY_SERIES",
    "UNSHACKLE_LEGACY_TRACKS",
    "UNSHACKLE_V2",
    "load",
]

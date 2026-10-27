"""Manifest/playlist source documents, never serialized downloader state."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from io import StringIO
from typing import Any

BUNDLE_MARKER = "_unidl_manifest_sources"
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
SEGMENT_ENCODING = "unidl-columns-v1"
MAX_TABLE_SEGMENTS = 1_000_000


class ManifestSourceError(ValueError):
    pass


def source_metadata(value: Mapping[str, Any]) -> dict[str, str]:
    """Only public provenance, never provider responses or licence context."""
    return {
        key: value[key] for key in ("url", "requested", "returned", "note", "profile", "kind", "api")
        if isinstance(value.get(key), str) and value[key]
    }


def merge_source_metadata(*inventories) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for inventory in inventories:
        for value in inventory or ():
            if isinstance(value, Mapping):
                item = source_metadata(value)
                if item and item not in result:
                    result.append(item)
    return result


def subtitle_source_identity(value: Mapping[str, Any]) -> str:
    """Selection/name/provenance do not change the underlying subtitle source."""
    return json.dumps({
        "url": value["url"], "language": value.get("language") or "und",
        "kind": value.get("kind") or "normal", "format": value.get("format") or "",
        "headers": {key.casefold(): item for key, item in (value.get("headers") or {}).items()},
    }, sort_keys=True)


def with_subtitle_sources(document: Mapping[str, Any], subtitles: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep independent API subtitle URLs alongside, not inside, MPD/HLS state."""
    if not subtitles:
        return dict(document)
    result = (
        dict(document) if BUNDLE_MARKER in document
        else source_bundle([{"type": "json", "document": source_manifest(document)}], mode="collection")
    )
    validate_bundle({**result, "subtitle_sources": subtitles})
    rows: dict[str, dict[str, Any]] = {}
    for item in [*result.get("subtitle_sources", []), *subtitles]:
        identity = subtitle_source_identity(item)
        previous = rows.get(identity)
        if previous is not None:
            item = {**item, "sources": merge_source_metadata(previous.get("sources"), item.get("sources"))}
        rows[identity] = item
    result["subtitle_sources"] = list(rows.values())
    validate_bundle(result)
    return result


def source_request_headers(extra: Mapping[str, Any], current: dict[str, str] | None = None) -> dict[str, str] | None:
    """Carry a saved source's request context through native download/hydration."""
    saved = extra.get("_unidl_source_headers")
    scoped = extra.get("_unidl_subtitle_headers")
    if not saved and not scoped:
        return current
    result = dict(saved or {})
    for headers in (current, scoped):
        for name, value in (headers or {}).items():
            for old in list(result):
                if old.casefold() == name.casefold():
                    del result[old]
            result[name] = value
    return result


# Legacy decoding only. New manifests never encode or persist segment tables.
def _column_values(column: Any, count: int) -> Iterator[Any]:
    if not isinstance(column, dict):
        raise ValueError("invalid segment table column")
    keys = set(column) - {"missing"}
    if keys == {"values"}:
        values = column["values"]
        if not isinstance(values, list) or len(values) != count:
            raise ValueError("segment table column length does not match count")
        yield from values
    elif keys in ({"start", "step"}, {"start", "step", "overrides"}):
        if any(type(column[key]) is not int for key in ("start", "step")):
            raise ValueError("segment table progression must use integers")
        overrides = column.get("overrides", [])
        if not isinstance(overrides, list) or any(
            not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int or not 0 <= row[0] < count
            or type(row[1]) is not int for row in overrides
        ) or len({row[0] for row in overrides}) != len(overrides):
            raise ValueError("invalid segment table progression overrides")
        exceptions = dict(overrides)
        for index in range(count):
            yield exceptions.get(index, column["start"] + column["step"] * index)
    elif keys == {"runs"}:
        runs = column["runs"]
        if not isinstance(runs, list) or any(
            not isinstance(run, list) or len(run) != 2 or type(run[0]) is not int or run[0] <= 0
            for run in runs
        ) or sum(run[0] for run in runs) != count:
            raise ValueError("segment table run lengths do not match count")
        for length, value in runs:
            for _ in range(length):
                yield value
    elif keys in ({"range_start", "lengths"}, {"range_start", "lengths", "gaps"}):
        start, lengths = column["range_start"], column["lengths"]
        if type(start) is not int or start < 0 or not isinstance(lengths, list) or len(lengths) != count or any(
            type(length) is not int or length <= 0 for length in lengths
        ):
            raise ValueError("invalid segment table byte ranges")
        gaps = _column_values(column["gaps"], count - 1) if "gaps" in column else None
        for index, length in enumerate(lengths):
            if start < 0:
                raise ValueError("invalid segment table byte-range start")
            yield [start, start + length - 1]
            start += length
            if gaps is not None and index + 1 < count:
                gap = next(gaps)
                if type(gap) is not int:
                    raise ValueError("invalid segment table byte-range gap")
                start += gap
    else:
        raise ValueError("unsupported segment table column encoding")


def expand_segments(table: Any) -> Iterator[dict[str, Any]]:
    """Validate and expand a table without allocating another full row list."""
    if not isinstance(table, dict) or table.get("encoding") != SEGMENT_ENCODING:
        raise ValueError("unsupported compact segment table encoding")
    if set(table) != {"encoding", "count", "defaults", "columns"}:
        raise ValueError("invalid compact segment table fields")
    count, defaults, columns = table["count"], table["defaults"], table["columns"]
    if type(count) is not int or not 0 < count <= MAX_TABLE_SEGMENTS:
        raise ValueError("segment table count is outside the supported bounds")
    if not isinstance(defaults, dict) or not isinstance(columns, dict) or set(defaults) & set(columns):
        raise ValueError("invalid compact segment table defaults")
    if any(not isinstance(key, str) for key in (*defaults, *columns)):
        raise ValueError("invalid compact segment table column name")
    iterators = {}
    missing_by_key = {}
    for key, column in columns.items():
        if not isinstance(column, dict):
            raise ValueError("invalid segment table column")
        missing = column.get("missing", [])
        if not isinstance(missing, list) or any(type(index) is not int or not 0 <= index < count for index in missing):
            raise ValueError("invalid segment table missing-value indexes")
        if len(set(missing)) != len(missing):
            raise ValueError("duplicate segment table missing-value indexes")
        missing_by_key[key] = set(missing)
        iterators[key] = _column_values(column, count)
    for index in range(count):
        row = dict(defaults)
        for key, values in iterators.items():
            value = next(values)
            if index not in missing_by_key[key]:
                row[key] = value
        yield row


def source_manifest(document: Mapping[str, Any]) -> dict[str, Any]:
    """Reject expanded snapshots instead of silently stripping required data.

    Raw HLS/MPD/ISM text is a source protocol document and remains intact. JSON
    segment arrays/tables and old source-timeline snapshots are download state;
    existing readers can consume them, but new writers must not produce them.
    """
    if not isinstance(document, Mapping):
        raise ManifestSourceError("A source manifest must be a JSON object.")
    stack: list[Any] = [document]
    seen: set[int] = set()
    while stack:
        value = stack.pop()
        if isinstance(value, (Mapping, list, tuple)):
            if id(value) in seen:
                continue
            seen.add(id(value))
        if isinstance(value, Mapping):
            if "segments" in value or value.get("encoding") == "unidl-columns-v1":
                raise ManifestSourceError(
                    "This input contains expanded segment state, not a source manifest/playlist. "
                    "Create it again from the original manifest. No tracks or segments were silently discarded."
                )
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    return dict(document)


def validate_bundle(document: Mapping[str, Any]) -> None:
    """Validate the source envelope before any URL is treated as media."""
    if type(document.get(BUNDLE_MARKER)) is not int or document[BUNDLE_MARKER] != 1:
        raise ManifestSourceError("Unsupported manifest-source format version.")
    if not isinstance(document.get("mode"), str) or document["mode"] not in {"merge", "timeline", "collection"}:
        raise ManifestSourceError("Unsupported manifest-source merge mode.")
    sources = document.get("sources")
    if not isinstance(sources, list) or not 0 < len(sources) <= 1024:
        raise ManifestSourceError("A source manifest requires 1–1024 source records.")
    duration = document.get("duration", 0)
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise ManifestSourceError("Invalid source timeline duration.")
    tracks = document.get("tracks", [])
    if "split_muxed_audio" in document and type(document["split_muxed_audio"]) is not bool:
        raise ManifestSourceError("Invalid muxed A/V separation policy.")
    if not isinstance(tracks, list) or not all(isinstance(track, dict) for track in tracks):
        raise ManifestSourceError("Invalid source track metadata.")
    subtitles = document.get("subtitle_sources", [])
    if not isinstance(subtitles, list):
        raise ManifestSourceError("Invalid independent subtitle sources.")
    for subtitle in subtitles:
        if not isinstance(subtitle, dict) or not isinstance(subtitle.get("url"), str) or not subtitle["url"].strip():
            raise ManifestSourceError("An independent subtitle source requires a URL.")
        for key in ("language", "kind", "name", "format"):
            if key in subtitle and not isinstance(subtitle[key], str):
                raise ManifestSourceError("Invalid independent subtitle metadata.")
        for key in ("original", "selected"):
            if key in subtitle and type(subtitle[key]) is not bool:
                raise ManifestSourceError("Invalid independent subtitle flag.")
        if "headers" in subtitle and (
            not isinstance(subtitle["headers"], dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in subtitle["headers"].items())
        ):
            raise ManifestSourceError("Invalid independent subtitle request headers.")
        if "sources" in subtitle and (
            not isinstance(subtitle["sources"], list)
            or not all(isinstance(item, dict) and item == source_metadata(item) for item in subtitle["sources"])
        ):
            raise ManifestSourceError("Invalid independent subtitle provenance.")
    for record in sources:
        if not isinstance(record, dict) or not isinstance(record.get("type"), str) or record["type"] not in {"reference", "json", "hls", "dash", "ism"}:
            raise ManifestSourceError("Unsupported manifest source type.")
        kind = record["type"]
        if kind == "reference" and (not isinstance(record.get("url"), str) or not record["url"].strip()):
            raise ManifestSourceError("A manifest reference needs a source URL/path.")
        if kind == "json" and not isinstance(record.get("document"), dict):
            raise ManifestSourceError("A JSON manifest source needs its original document.")
        if kind in {"hls", "dash", "ism"} and (not isinstance(record.get("text"), str) or not record["text"].strip()):
            raise ManifestSourceError("An inline manifest source needs its original protocol text.")
        if "base_url" in record and not isinstance(record["base_url"], str):
            raise ManifestSourceError("Invalid manifest source base URL.")
        if "headers" in record and (
            not isinstance(record["headers"], dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in record["headers"].items())
        ):
            raise ManifestSourceError("Invalid manifest source request headers.")
        for flag in ("details", "no_probe", "append_url_params"):
            if flag in record and type(record[flag]) is not bool:
                raise ManifestSourceError("Invalid manifest source parser policy.")
        if "track" in record and not isinstance(record["track"], dict):
            raise ManifestSourceError("Invalid media playlist track metadata.")
        if "metadata" in record and (
            not isinstance(record["metadata"], dict) or record["metadata"] != source_metadata(record["metadata"])
        ):
            raise ManifestSourceError("Invalid manifest source provenance.")
        if "session_keys" in record and (
            not isinstance(record["session_keys"], list)
            or not all(isinstance(value, str) and value.startswith("#EXT-X-SESSION-KEY:") for value in record["session_keys"])
        ):
            raise ManifestSourceError("Invalid HLS session-key metadata.")
        if "export_entry" in record and (kind != "reference" or type(record["export_entry"]) is not int or record["export_entry"] < 0):
            raise ManifestSourceError("Invalid legacy export source reference.")
    source_manifest(document)


def source_bundle(sources: list[dict[str, Any]], *, mode: str = "merge", duration: float = 0,
                  tracks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not sources or mode not in {"merge", "timeline", "collection"}:
        raise ManifestSourceError("A manifest collection needs source manifests and a supported merge mode.")
    result: dict[str, Any] = {BUNDLE_MARKER: 1, "mode": mode, "sources": sources}
    if mode == "timeline":
        result["duration"] = duration
    if tracks:
        result["tracks"] = tracks
    validate_bundle(result)
    return source_manifest(result)


def manifest_json(document: Mapping[str, Any]) -> str:
    """Bound deterministic source JSON before writing a cache file."""
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    size = 0
    with StringIO() as buffer:
        try:
            for chunk in encoder.iterencode(source_manifest(document)):
                size += len(chunk.encode("utf-8"))
                if size > MAX_MANIFEST_BYTES:
                    raise ManifestSourceError(
                        "Generated manifest exceeds the 32 MiB safety limit; no cache file was written. "
                        "No sources or tracks were truncated."
                    )
                buffer.write(chunk)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ManifestSourceError):
                raise
            raise ManifestSourceError("Manifest contains unsupported JSON data; no cache file was written.") from exc
        return buffer.getvalue()

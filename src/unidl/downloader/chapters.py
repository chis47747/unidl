"""Read Core chapter sidecars and render muxer-specific metadata."""

from __future__ import annotations

import json
from pathlib import Path

from ..core.chapters import Chapter, normalize_chapters, timestamp


class ChapterFileError(ValueError):
    """A chapter sidecar cannot be represented safely."""


def load_chapters_file(path: str | Path) -> tuple[Chapter, ...]:
    source = Path(path)
    try:
        document = json.loads(source.read_text("utf-8"))
    except OSError as exc:
        raise ChapterFileError(f"Could not read chapters file {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ChapterFileError(
            f"Invalid chapter JSON in {source}: {exc.msg} at line {exc.lineno}, column {exc.colno}."
        ) from exc
    if not isinstance(document, dict) or document.get("kind") != "unidl-chapters":
        raise ChapterFileError("Chapter JSON is not a UniDL chapter document.")
    if document.get("version") != 1:
        raise ChapterFileError(
            f"Unsupported chapter document version: {document.get('version')!r}."
        )
    records = document.get("chapters")
    if not isinstance(records, list):
        raise ChapterFileError("Chapter JSON needs a chapters list.")
    if not records:
        raise ChapterFileError("Chapter JSON does not contain any chapters.")
    try:
        return normalize_chapters(Chapter.from_document(record) for record in records)
    except (TypeError, ValueError) as exc:
        raise ChapterFileError(f"Invalid chapter entry: {exc}") from exc


def write_ffmetadata(chapters: tuple[Chapter, ...], path: str | Path) -> Path:
    """Write FFmpeg's metadata demuxer chapter syntax."""
    target = Path(path)
    lines = [";FFMETADATA1"]
    for chapter in chapters:
        # FFmetadata requires END.  A start-only final marker remains useful for
        # navigation; one millisecond is the smallest honest bounded interval.
        end = chapter.end_ms if chapter.end_ms is not None else chapter.start_ms + 1
        lines.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={chapter.start_ms}",
                f"END={max(chapter.start_ms + 1, end)}",
                f"title={_escape_ffmetadata(chapter.title)}",
            ]
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_ogm_chapters(chapters: tuple[Chapter, ...], path: str | Path) -> Path:
    """Write the simple OGM chapter syntax accepted by mkvmerge."""
    target = Path(path)
    width = max(2, len(str(len(chapters))))
    lines: list[str] = []
    for index, chapter in enumerate(chapters, start=1):
        label = f"{index:0{width}d}"
        lines.append(f"CHAPTER{label}={timestamp(chapter.start_ms, precise=True)}")
        lines.append(f"CHAPTER{label}NAME={chapter.title}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _escape_ffmetadata(value: str) -> str:
    # Backslash first, otherwise the escapes introduced below are escaped again.
    result = str(value).replace("\\", "\\\\")
    for character in ("=", ";", "#"):
        result = result.replace(character, f"\\{character}")
    return result.replace("\n", "\\n").replace("\r", "")


__all__ = [
    "ChapterFileError",
    "load_chapters_file",
    "write_ffmetadata",
    "write_ogm_chapters",
]

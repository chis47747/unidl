"""Optional chapters and artwork from NRK playback metadata."""

from __future__ import annotations

import logging
import math
import re
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ...core.attachments import Attachment
from ...core.chapters import Chapter, normalize_chapters

log = logging.getLogger(__name__)
_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?"
)


def _duration_ms(value: Any) -> int:
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if match is None or not any(match.groupdict().values()) or value.endswith("T"):
        raise ValueError("invalid ISO 8601 duration")
    seconds = sum(
        Decimal(match.group(name) or 0) * scale
        for name, scale in (("days", 86400), ("hours", 3600), ("minutes", 60), ("seconds", 1))
    )
    return round(seconds * 1000)


def _skip_ms(info: dict[str, Any], name: str) -> int:
    # The response exposes ISO durations and the older explicit-seconds fields.
    if info.get(name) is not None:
        return _duration_ms(info[name])
    value = info.get(f"{name}InSeconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid skip position in seconds")
    return round(value * 1000)


def chapters(raw: dict[str, Any]) -> list[Chapter]:
    result: list[Chapter] = []
    try:
        duration = _duration_ms(raw.get("duration"))
    except ValueError:
        duration = None

    def add(start: int, title: str, *, end: int | None = None, kind: str = "") -> None:
        chapter = Chapter(start_ms=start, end_ms=end, title=title, kind=kind)
        if duration is not None and (start >= duration or (end is not None and end > duration)):
            raise ValueError("chapter outside programme duration")
        result.append(chapter)

    preplay = raw.get("preplay")
    points = preplay.get("indexPoints") if isinstance(preplay, dict) else None
    if points is not None and not isinstance(points, list):
        log.warning("NRK chapter index is not a list")
    for point in points if isinstance(points, list) else ():
        try:
            if not isinstance(point, dict) or not isinstance(point.get("title"), str):
                raise ValueError("invalid chapter index entry")
            add(_duration_ms(point.get("startPoint")), point["title"])
        except (TypeError, ValueError, OverflowError):
            log.warning("NRK skipped an invalid chapter index entry")

    skip = raw.get("skipDialogInfo")
    if skip is not None and not isinstance(skip, dict):
        log.warning("NRK skip metadata is not an object")
    if isinstance(skip, dict):
        if any(
            skip.get(key) is not None for key in ("startIntro", "startIntroInSeconds", "endIntro", "endIntroInSeconds")
        ):
            try:
                start, end = _skip_ms(skip, "startIntro"), _skip_ms(skip, "endIntro")
                if start != 0 or end != 0:
                    add(start, "Intro", end=end, kind="intro")
            except (TypeError, ValueError, OverflowError):
                log.warning("NRK skipped an invalid intro range")
        if any(skip.get(key) is not None for key in ("startCredits", "startCreditsInSeconds")):
            try:
                start = _skip_ms(skip, "startCredits")
                if start > 0:
                    add(start, "Credits", kind="credits")
            except (TypeError, ValueError, OverflowError):
                log.warning("NRK skipped an invalid credits position")
    return list(normalize_chapters(result, duration_ms=duration))


def attachments(info: dict[str, Any], raw: dict[str, Any]) -> list[Attachment]:
    result: list[Attachment] = []
    seen: set[str] = set()

    def add_images(images: Any, label: str, kind: str) -> None:
        if not isinstance(images, list):
            return
        for image in images:
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                continue
            url = image["url"].strip()
            try:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"https", "http"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or any(char.isspace() for char in url)
                ):
                    continue
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            width = image.get("pixelWidth", image.get("width"))
            suffix = f" · {width}px" if type(width) is int and width > 0 else ""
            mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".avif": "image/avif",
            }.get(PurePosixPath(parsed.path).suffix.lower(), "")
            result.append(Attachment(url=url, name=label + suffix, kind=kind, mime_type=mime))

    preplay = raw.get("preplay")
    if isinstance(preplay, dict):
        poster = preplay.get("poster")
        if isinstance(poster, dict):
            add_images(poster.get("images"), "Poster", "poster")
    program = info.get("programInformation")
    if isinstance(program, dict):
        add_images(program.get("image"), "Programme artwork", "artwork")
    image = info.get("image")
    add_images(image.get("webImages") if isinstance(image, dict) else image, "Artwork", "artwork")
    points = preplay.get("indexPoints") if isinstance(preplay, dict) else None
    for index, point in enumerate(points if isinstance(points, list) else (), 1):
        if isinstance(point, dict):
            title = point.get("title")
            label = f"Chapter {index} · {title}" if isinstance(title, str) and title.strip() else f"Chapter {index}"
            add_images(point.get("thumbnails"), label, "thumbnail")
    return result

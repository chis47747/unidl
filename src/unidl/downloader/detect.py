from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .utils import is_url

DIRECT_EXTENSIONS = {"mp3", "mp4", "m4a", "m4v", "aac", "flac", "wav", "ts", "m2ts", "mov"}


def guess_kind(source: str, content: str | None = None) -> str:
    lowered_source = source.lower()
    parsed = urlparse(lowered_source) if is_url(lowered_source) else None
    path = parsed.path if parsed else lowered_source
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower().lstrip(".")

    if content:
        stripped = content.lstrip("\ufeff\r\n\t ")
        upper = stripped[:500].upper()
        if upper.startswith("#EXTM3U"):
            return "hls"
        if "<MPD" in stripped[:1000] or ":MPD" in stripped[:1000]:
            return "dash"
        if "<SMOOTHSTREAMINGMEDIA" in upper:
            return "ism"
        if stripped.startswith("{") or stripped.startswith("["):
            return "json"

    if path.endswith(".ism/manifest") or path.endswith(".isml/manifest") or path.endswith("/manifest"):
        return "ism"
    if name in {".mpd", ".dash"}:
        return "dash"
    if suffix in {"m3u8", "m3u"}:
        return "hls"
    if suffix in {"mpd", "dash"}:
        return "dash"
    if suffix == "json":
        return "json"
    if suffix in {"ism", "isml"} or ".ism/" in path or ".isml/" in path:
        return "ism"
    if suffix in DIRECT_EXTENSIONS:
        return "direct"

    return "direct"

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from .loader import LoadError, load_bytes
from .postprocess import _run_external
from .utils import unique_path

ID3_METADATA_FIELDS = (
    "title",
    "artist",
    "album",
    "album_artist",
    "date",
    "track",
    "disc",
    "genre",
    "composer",
    "comment",
    "copyright",
    "publisher",
    "isrc",
    "lyrics",
)

_COVER_LOCK = threading.Lock()


def audio_metadata_from_title(title: dict[str, Any]) -> dict[str, Any]:
    supplied = title.get("audio_metadata")
    raw = supplied if isinstance(supplied, dict) else {}
    aliases = {
        "title": (raw.get("title"), title.get("name"), title.get("title")),
        "artist": (raw.get("artist"), title.get("artist")),
        "album": (raw.get("album"), title.get("album")),
        "album_artist": (raw.get("album_artist"), raw.get("albumArtist"), title.get("album_artist"), title.get("albumArtist")),
        "date": (raw.get("date"), raw.get("year"), title.get("release_date"), title.get("releaseDate"), title.get("year")),
        "track": (raw.get("track"), raw.get("track_number"), raw.get("trackNumber"), title.get("track"), title.get("track_number")),
        "disc": (raw.get("disc"), raw.get("disc_number"), raw.get("discNumber"), title.get("disc"), title.get("disc_number")),
        "genre": (raw.get("genre"), title.get("genre")),
        "composer": (raw.get("composer"), title.get("composer")),
        "comment": (raw.get("comment"), title.get("comment")),
        "copyright": (raw.get("copyright"), title.get("copyright")),
        "publisher": (raw.get("publisher"), title.get("publisher")),
        "isrc": (raw.get("isrc"), raw.get("ISRC"), title.get("isrc"), title.get("ISRC")),
        "lyrics": (raw.get("lyrics"), title.get("lyrics")),
    }
    metadata: dict[str, Any] = {}
    for name, values in aliases.items():
        for item in values:
            value = _metadata_value(item)
            if value:
                metadata[name] = value
                break

    cover = raw.get("cover")
    if cover is None:
        cover = title.get("cover")
    if cover is None:
        cover_url = raw.get("cover_url") or raw.get("coverUrl") or title.get("cover_url") or title.get("coverUrl")
        if cover_url:
            cover = {"url": cover_url}
    if isinstance(cover, str) and cover.strip():
        cover = {"url": cover.strip()}
    if isinstance(cover, dict):
        normalized_cover = {
            key: value
            for key, value in cover.items()
            if key in {"url", "path", "headers", "mime_type", "mimeType"} and value is not None and value != ""
        }
        if normalized_cover.get("url") or normalized_cover.get("path"):
            metadata["cover"] = normalized_cover
    return metadata


def load_audio_metadata_file(input_path: str | Path) -> dict[str, Any]:
    path = Path(input_path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid audio metadata JSON in {path}: {exc.msg} at line {exc.lineno}, column {exc.colno}.") from exc
    except OSError as exc:
        raise RuntimeError(f"Failed to read audio metadata file {path}: {exc}") from exc
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("Audio metadata JSON root must be an object or a one-item object array.")

    title = payload if isinstance(payload.get("audio_metadata"), dict) else {"audio_metadata": payload}
    metadata = audio_metadata_from_title(title)
    if not metadata:
        raise ValueError("Audio metadata JSON does not contain any supported metadata or cover fields.")
    cover = metadata.get("cover")
    if isinstance(cover, dict):
        cover = dict(cover)
        source_key = "path" if cover.get("path") else "url"
        source = str(cover.get(source_key) or "").strip()
        if source and not urlparse(source).scheme:
            candidate = Path(source).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            cover[source_key] = str(candidate.resolve())
        metadata["cover"] = cover
    return metadata


def audio_metadata_payload(stream) -> dict[str, Any]:
    extra = getattr(stream, "extra", None)
    if not isinstance(extra, dict):
        return {}
    metadata = extra.get("audio_metadata")
    return metadata if isinstance(metadata, dict) else {}


def audio_metadata_signature(stream) -> str | None:
    metadata = audio_metadata_payload(stream)
    if not metadata:
        return None
    payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def audio_id3_metadata(stream) -> dict[str, str]:
    raw = audio_metadata_payload(stream)
    return {
        name: str(raw[name]).strip()
        for name in ID3_METADATA_FIELDS
        if raw.get(name) is not None and str(raw[name]).strip()
    }


def prepare_audio_cover(
    stream,
    cover_dir: str | Path,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    retries: int = 3,
) -> Path | None:
    cover = audio_metadata_payload(stream).get("cover")
    if not isinstance(cover, dict):
        return None
    source = str(cover.get("path") or cover.get("url") or "").strip()
    if not source:
        return None
    source = _resolve_cover_source(source, stream)
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        path = Path(unquote(parsed.path) if parsed.scheme == "file" else source).expanduser()
        if not path.is_file():
            raise RuntimeError(f"Audio cover file not found: {path}")
        return path

    cover_headers = dict(headers or {})
    supplied_headers = cover.get("headers")
    if isinstance(supplied_headers, dict):
        cover_headers.update({str(name): str(value) for name, value in supplied_headers.items() if str(name).strip()})
    cache_key = json.dumps([source, sorted(cover_headers.items())], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
    directory = Path(cover_dir).expanduser()
    with _COVER_LOCK:
        existing = next((path for path in directory.glob(f"{digest}.*") if path.is_file()), None)
        if existing is not None:
            return existing
        try:
            resource = load_bytes(source, headers=cover_headers, timeout=max(1, timeout), retries=max(1, retries))
        except LoadError as exc:
            raise RuntimeError(f"Failed to download audio cover: {exc}") from exc
        data = resource.bytes_data or b""
        suffix = _cover_suffix(data)
        if suffix is None:
            raise RuntimeError("Audio cover is not a supported JPEG, PNG, or WebP image.")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{digest}{suffix}"
        pending = directory / f"{digest}.{threading.get_ident()}.tmp"
        pending.write_bytes(data)
        os.replace(pending, target)
        return target


def transcode_audio(
    input_path: str | Path,
    output_format: str,
    output_path: str | Path | None = None,
    metadata: dict[str, str] | None = None,
    cover_path: str | Path | None = None,
    copy_audio: bool = False,
) -> Path:
    target_format = str(output_format or "").strip().lower()
    if target_format not in {"mp3", "flac", "alac", "m4a"}:
        raise ValueError(f"Unsupported audio output format: {output_format}")
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg not found; lossless audio conversion needs ffmpeg.")
    input_path = Path(input_path)
    suffix = {"mp3": ".mp3", "flac": ".flac", "alac": ".m4a", "m4a": ".m4a"}[target_format]
    output = Path(output_path) if output_path else unique_path(input_path.with_suffix(suffix))
    if output.resolve() == input_path.resolve():
        output = unique_path(input_path.with_name(f"{input_path.stem}.converted{suffix}"))
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [executable, "-hide_banner", "-nostdin", "-y", "-i", str(input_path)]
    cover = Path(cover_path) if cover_path else None
    if cover is not None:
        args.extend(["-i", str(cover)])
    args.extend(["-map", "0:a:0"])
    if cover is not None:
        args.extend(
            [
                "-map",
                "1:v:0",
                "-c:v",
                "mjpeg",
                "-q:v",
                "2",
                "-disposition:v:0",
                "attached_pic",
                "-metadata:s:v:0",
                "title=Album cover",
                "-metadata:s:v:0",
                "comment=Cover (front)",
            ]
        )
    else:
        args.append("-vn")
    args.extend(["-sn", "-dn", "-map_metadata", "0"])
    for name, value in (metadata or {}).items():
        if value:
            args.extend(["-metadata", f"{name}={value}"])
    if copy_audio or target_format == "m4a":
        args.extend(["-c:a", "copy"])
    elif target_format == "flac":
        args.extend(["-c:a", "flac"])
    elif target_format == "alac":
        args.extend(["-c:a", "alac"])
    else:
        args.extend(["-c:a", "libmp3lame", "-b:a", "320k"])
    if target_format == "mp3":
        args.extend(["-id3v2_version", "3"])
    elif target_format == "m4a":
        # The extension selects ffmpeg's legacy ``ipod`` muxer, which rejects
        # E-AC-3 even though the MP4 container supports it. Force the modern MP4
        # muxer while retaining the ordinary player-friendly .m4a extension.
        args.extend(["-f", "mp4"])
    args.append(str(output))
    labels = {
        "mp3": "MP3 audio transcoding",
        "flac": "FLAC audio conversion",
        "alac": "ALAC audio conversion",
        "m4a": "M4A audio remux",
    }
    _run_external(args, labels[target_format])
    return output


def _metadata_value(value: Any) -> str | None:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "; ".join(items) or None
    if value is None or isinstance(value, (dict, set)):
        return None
    text = str(value).strip()
    return text or None


def _resolve_cover_source(source: str, stream) -> str:
    parsed = urlparse(source)
    if parsed.scheme:
        return source
    local_path = Path(source).expanduser()
    if local_path.is_absolute():
        return str(local_path)
    original = str(getattr(stream, "original_url", None) or "")
    original_parsed = urlparse(original)
    if original_parsed.scheme in {"http", "https"}:
        return urljoin(original, source)
    if original:
        return str((Path(original).expanduser().resolve().parent / source).resolve())
    return str(Path(source).expanduser().resolve())


def _cover_suffix(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None

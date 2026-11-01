from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

# Windows' traditional path APIs reject paths at 260 characters. Keep a
# margin for temporary/decryption suffixes added after a track filename is
# chosen. Long-path-aware installations keep their original names until this
# limit is actually needed.
WINDOWS_PATH_LIMIT = 240
# Generated paths gain additional components/suffixes during download and
# post-processing (``vod``, ``sections``, ``.dec``, ``.tmp``).  Keep a second
# margin below the final safe limit so those derived paths remain creatable on
# Windows even when the original output title is very long.
WINDOWS_INTERMEDIATE_PATH_LIMIT = 200


def bounded_path(path: str | Path, *, max_length: int = WINDOWS_PATH_LIMIT, force: bool = False) -> Path:
    """Shorten only the final path component when Windows needs it.

    The complete original path is part of the digest, so two long titles do
    not collapse onto the same resume/output name. ``force`` is available for
    platform-independent regression tests; production calls use the host
    platform automatically.
    """
    target = Path(path)
    if not force and os.name != "nt":
        return target
    text = str(target)
    if len(text) <= max_length:
        return target
    parent = target.parent
    suffix = target.suffix
    stem = target.name[: -len(suffix)] if suffix else target.name
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
    available = max_length - len(str(parent)) - len(suffix) - len(digest) - 2
    if available < 1:
        # The parent itself is too long. There is no safe filename-only
        # rewrite; the caller can choose a shorter output directory or enable
        # Win32 long paths.
        return target
    return parent / f"{stem[:available]}_{digest}{suffix}"


def safe_temp_root(
    path: str | Path,
    *,
    max_length: int = WINDOWS_INTERMEDIATE_PATH_LIMIT,
    force: bool = False,
) -> Path:
    """Return a root that leaves room for UniDL's generated temp children.

    A filename-only shortening cannot repair a temp directory whose parent is
    already close to MAX_PATH.  On Windows, keep a deterministic, compact
    mirror under the system temp directory in that case.  The digest preserves
    isolation between configured roots and keeps resume caches stable across
    runs.  ``force`` is used by platform-independent path regression tests.
    """
    root = Path(path).expanduser()
    if not force and os.name != "nt":
        return root
    text = os.path.abspath(os.fspath(root))
    # Leave enough room for a task/track directory, a section directory, and a
    # generated filename/suffix.  This is intentionally stricter than the
    # final output limit because temp paths are nested several levels deep.
    if len(text) <= max_length - 72:
        return root
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    compact = Path(tempfile.gettempdir()) / "unidl" / digest
    return compact


def bounded_child_path(
    parent: str | Path,
    name: str,
    *,
    max_length: int = WINDOWS_PATH_LIMIT,
    force: bool = False,
) -> Path:
    """Build a child path while preserving its extension and stable identity."""
    target = bounded_path(Path(parent) / name, max_length=max_length, force=force)
    if (force or os.name == "nt") and len(str(target)) > max_length:
        # The parent itself may already exceed the budget.  This helper is for
        # generated children (temp/section directories), so it is safe to use
        # the deterministic compact temp mirror instead of returning a path
        # that Windows cannot create.
        compact_parent = safe_temp_root(parent, max_length=max_length, force=force)
        target = bounded_path(compact_parent / name, max_length=max_length, force=force)
    return target


def windows_long_path(path: str | Path) -> str:
    """Return an extended-length Windows path for cleanup operations."""
    text = os.path.abspath(os.fspath(path))
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https"}


def is_file_url(value: str) -> bool:
    return urlparse(value).scheme == "file"


def source_path(value: str) -> Path:
    if is_file_url(value):
        return Path(unquote(urlparse(value).path))
    path = Path(value).expanduser()
    if path.exists():
        return path
    unescaped = _unescape_shell_path(value)
    if unescaped != value:
        candidate = Path(unescaped).expanduser()
        if candidate.exists():
            return candidate
    return path


def _unescape_shell_path(value: str) -> str:
    result: list[str] = []
    index = 0
    escapable = set(" \t\n()[]{}&;\"'`$\\|<>*?!#~")
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in escapable:
            result.append(value[index + 1])
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def join_uri(base_uri: str, reference: str | None) -> str:
    if not reference:
        return base_uri
    reference = reference.strip()
    if is_url(reference) or is_file_url(reference):
        return reference
    if is_url(base_uri):
        return urljoin(base_uri, reference)
    if is_file_url(base_uri):
        base_path = source_path(base_uri)
        return _path_uri_string((base_path.parent / reference).resolve())

    base_path = source_path(base_uri)
    if base_path.is_dir():
        return _path_uri_string((base_path / reference).resolve())
    return _path_uri_string((base_path.parent / reference).resolve())


def _path_uri_string(path: Path) -> str:
    return path.as_posix()


def uri_basename(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https", "file"}:
        path = parsed.path
    else:
        path = value
    name = Path(unquote(path)).name
    return name or value.rstrip("/").rsplit("/", 1)[-1] or value


def compact_join(parts: list[str | None], separator: str = " | ") -> str:
    cleaned = [part.strip() for part in parts if part and str(part).strip()]
    return separator.join(cleaned)


_LANGUAGE_REGION = re.compile(
    r"(?:[A-Za-z]{2,3}(?:-[A-Za-z]{3}){0,3}|[A-Za-z]{4}|[A-Za-z]{5,8})"
    r"(?:-[A-Za-z]{4})?-(?P<region>[A-Za-z]{2}|[0-9]{3})"
    r"(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
    r"(?:-[0-9A-WY-Za-wy-z](?:-[A-Za-z0-9]{2,8})+)*"
    r"(?:-[xX](?:-[A-Za-z0-9]{1,8})+)?"
)


@lru_cache(maxsize=1)
def _region_names() -> dict[str, str]:
    # UN M49 macroregions are regions, not countries. In particular, es-419
    # must not be guessed as Spain or any single Latin American country.
    names = {
        "001": "World",
        "002": "Africa",
        "003": "North America",
        "005": "South America",
        "009": "Oceania",
        "011": "Western Africa",
        "013": "Central America",
        "014": "Eastern Africa",
        "015": "Northern Africa",
        "017": "Middle Africa",
        "018": "Southern Africa",
        "019": "Americas",
        "021": "Northern America",
        "029": "Caribbean",
        "030": "Eastern Asia",
        "034": "Southern Asia",
        "035": "South-eastern Asia",
        "039": "Southern Europe",
        "053": "Australia and New Zealand",
        "054": "Melanesia",
        "057": "Micronesia",
        "061": "Polynesia",
        "142": "Asia",
        "143": "Central Asia",
        "145": "Western Asia",
        "150": "Europe",
        "151": "Eastern Europe",
        "154": "Northern Europe",
        "155": "Western Europe",
        "202": "Sub-Saharan Africa",
        "419": "Latin America and the Caribbean",
    }
    # Load only the small, bundled country table, once and on first use. Track
    # rendering must not fetch location data or load the much larger city table.
    try:
        from geonamescache import GeonamesCache
    except ImportError:
        return names
    for code, country in GeonamesCache().get_countries().items():
        names[code] = country["name"]
        names[str(country["isonumeric"]).zfill(3)] = country["name"]
    return names


@lru_cache(maxsize=512)
def language_region_name(language: str | None) -> str | None:
    """English name of an explicit BCP-47 region, never inferred from language.

    Scripts, variants and extensions are not countries. A private-use tag such
    as en-x-us therefore has no region; zh-Hant-HK does. Keep the original tag
    untouched for selection, mux metadata, filenames and manifest round trips.
    """
    if not language:
        return None
    match = _LANGUAGE_REGION.fullmatch(language.strip().replace("_", "-"))
    if match is None:
        return None
    return _region_names().get(match.group("region").upper())


def track_region_name(language: str | None, *identifiers: str | None) -> str | None:
    """Prefer the language tag, then an unambiguous locale in track metadata.

    Some manifests say lang="en" but audioTrackId="en-us_dialog_0". The
    locale prefix is explicit region evidence, unlike a bare language. Only
    use identifiers starting with the same primary language; do not scan URLs,
    arbitrary words or role suffixes for things that look like country codes.
    """
    region = language_region_name(language)
    if region:
        return region
    primary = str(language or "").strip().replace("_", "-").split("-", 1)[0].casefold()
    if not re.fullmatch(r"[a-z]{2,8}", primary) or primary in {"und", "mul", "zxx"}:
        return None
    regions: set[str] = set()
    for identifier in identifiers:
        if not isinstance(identifier, str) or not identifier:
            continue
        candidate = identifier.strip()
        if candidate.replace("_", "-").split("-", 1)[0].casefold() != primary:
            continue
        # Keep a full BCP-47 tag when available; otherwise strip only the
        # provider's underscore-delimited track-role/representation suffix.
        region = language_region_name(candidate)
        if region is None:
            parts = candidate.split("_")
            # A locale can itself use underscores, e.g. zh_Hant_HK_dialog_0.
            # Six subtags cover language, up to three extlangs, script and region.
            for end in range(min(len(parts), 6), 0, -1):
                region = language_region_name("_".join(parts[:end]))
                if region:
                    break
        if region:
            regions.add(region)
    return next(iter(regions)) if len(regions) == 1 else None


@lru_cache(maxsize=512)
def language_name(language: str | None) -> str | None:
    """English ISO-639 name without guessing a regional variant or country."""
    if not language:
        return None
    tag = language.strip().replace("_", "-")
    if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", tag):
        return None
    primary = tag.split("-", 1)[0].lower()
    if len(primary) not in {2, 3}:
        return None
    # Imported on first use, so startup and manifest parsing remain independent
    # of the language-name database. It is local data, never a network lookup.
    try:
        import pycountry
    except ImportError:
        return None
    if len(primary) == 2:
        entry = pycountry.languages.get(alpha_2=primary)
    else:
        entry = pycountry.languages.get(alpha_3=primary) or pycountry.languages.get(bibliographic=primary)
    return getattr(entry, "common_name", None) or getattr(entry, "name", None)


_MUX_LANGUAGE_ALIASES = {
    "guoyu": "cmn",
    "mandarin": "cmn",
    "putonghua": "cmn",
    "国语": "cmn",
    "國語": "cmn",
    "普通话": "cmn",
    "普通話": "cmn",
    "cantonese": "yue",
    "yueyu": "yue",
    "粤语": "yue",
    "粵語": "yue",
    "sp": "es",
    "iw": "he",
    "in": "id",
    "ji": "yi",
    "jp": "ja",
    "cz": "cs",
    "gr": "el",
    "zh-chs": "zh-Hans",
    "zh-cht": "zh-Hant",
}

_MUX_LANGUAGE_TAG = re.compile(
    r"(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}(?:-[a-z]{4})?(?:-(?:[a-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*"
    r"(?:-[0-9a-wyz](?:-[a-z0-9]{2,8})+)*(?:-x(?:-[a-z0-9]{1,8})+)?"
    r"|x(?:-[a-z0-9]{1,8})+)",
    re.IGNORECASE | re.ASCII,
)

_MUX_GRANDFATHERED_TAGS = frozenset(
    "en-gb-oed i-ami i-bnn i-default i-enochian i-hak i-klingon i-lux i-mingo "
    "i-navajo i-pwn i-tao i-tay i-tsu sgn-be-fr sgn-be-nl sgn-ch-de art-lojban "
    "cel-gaulish no-bok no-nyn zh-guoyu zh-hakka zh-min zh-min-nan zh-xiang".split()
)


@lru_cache(maxsize=512)
def normalize_mux_language(language: str | None) -> str:
    """Normalize output metadata only, without changing source/selection tags.

    Muxers require language tags, not provider labels. Preserve full regional,
    script, variant and private-use tags; unknown primary codes or malformed
    tags become ``und`` rather than aborting an otherwise valid download.
    """
    normalized = (language or "").strip().replace("_", "-")
    alias = _MUX_LANGUAGE_ALIASES.get(normalized.casefold())
    if alias:
        return alias
    if normalized.casefold() in _MUX_GRANDFATHERED_TAGS:
        return normalized
    if not _MUX_LANGUAGE_TAG.fullmatch(normalized):
        return "und"
    primary = normalized.split("-", 1)[0].lower()
    if primary == "x" or (len(primary) == 3 and "qaa" <= primary <= "qtz"):
        return normalized
    import pycountry

    entry = (
        pycountry.languages.get(alpha_2=primary) if len(primary) == 2 else
        pycountry.languages.get(alpha_3=primary) or pycountry.languages.get(bibliographic=primary)
    )
    return normalized if entry is not None else "und"


def track_language_label(language: str | None, *identifiers: str | None) -> str | None:
    """Display language (explicit region), separate from the original tag."""
    name = language_name(language)
    region = track_region_name(language, *identifiers)
    if name and region:
        return f"{name} ({region})"
    return name or region


def format_bitrate(value: int | None) -> str | None:
    if value is None or value <= 0:
        return None
    if value >= 1_000_000:
        mbps = value / 1_000_000
        return f"{mbps:.2f}".rstrip("0").rstrip(".") + " Mbps"
    return f"{round(value / 1000)} Kbps"


def format_frame_rate(value: float | None) -> str | None:
    if value is None or value <= 0:
        return None
    return f"{value:.3f}".rstrip("0").rstrip(".")


def format_segments(count: int) -> str | None:
    if count <= 0:
        return None
    return f"{count} Segment" if count == 1 else f"{count} Segments"


def format_time(seconds: float | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    seconds_int = int(round(seconds))
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"~{hours}h{minutes:02d}m{secs:02d}s"
    return f"~{minutes:02d}m{secs:02d}s"


def format_size(size: int | None) -> str | None:
    if size is None or size <= 0:
        return None
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)}B"
    return f"{value:.2f}".rstrip("0").rstrip(".") + units[unit_index]


def parse_frame_rate(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_float = float(denominator)
            if denominator_float == 0:
                return None
            return float(numerator) / denominator_float
        return float(value)
    except ValueError:
        return None


def parse_iso8601_duration(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    pattern = re.compile(
        r"^P"
        r"(?:(?P<years>\d+(?:\.\d+)?)Y)?"
        r"(?:(?P<date_months>\d+(?:\.\d+)?)M)?"
        r"(?:(?P<weeks>\d+(?:\.\d+)?)W)?"
        r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
        r"(?:T"
        r"(?:(?P<hours>\d+(?:\.\d+)?)H)?"
        r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
        r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
        r")?$",
        re.IGNORECASE,
    )
    match = pattern.match(value)
    if not match:
        return None
    fields = match.groupdict()
    if not any(field is not None for field in fields.values()):
        return None
    years = float(fields["years"] or 0)
    date_months = float(fields["date_months"] or 0)
    if years or date_months:
        return None
    weeks = float(fields["weeks"] or 0)
    days = float(match.group("days") or 0)
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return (weeks * 7 + days) * 86400 + hours * 3600 + minutes * 60 + seconds


_H266_URL_MARKERS = (
    ".f323",
    "h266",
    "h.266",
    "vvc1",
    "vvi1",
    "/vvc",
    ".vvc",
    "cmaf6",
    "cmfv6",
    ".mp6",
)

_VIDEO_CODEC_PREFIXES = (
    "avc",
    "avc1",
    "avc3",
    "dva1",
    "dvav",
    "h264",
    "h.264",
    "hvc1",
    "hev1",
    "dvh1",
    "dvhe",
    "hevc",
    "h265",
    "h.265",
    "vp09",
    "vp9",
    "vp08",
    "vp8",
    "av01",
    "av1",
    "vvc1",
    "vvi1",
    "h266",
    "h.266",
    "vvc",
)


def _compact_codec(value: str) -> str:
    return value.lower().replace(".", "").replace("/", "").replace("-", "").replace(" ", "")


def looks_like_h266(*values: str | None) -> bool:
    for value in values:
        if not value:
            continue
        if pretty_codec(value, "video") == "H.266":
            return True
        if video_codec_family(value) == "vvc":
            return True
        lowered = value.lower()
        if any(marker in lowered for marker in _H266_URL_MARKERS):
            return True
    return False


def video_codec_family(value: str | None) -> str:
    text = _compact_codec(value or "")
    if any(token in text for token in ("vvc1", "vvi1", "h266", "vvc")):
        return "vvc"
    if any(token in text for token in ("hvc1", "hev1", "hevc", "h265", "dvh1", "dvhe")):
        return "hevc"
    if any(token in text for token in ("avc1", "avc3", "h264", "dva1", "dvav")):
        return "h264"
    if any(token in text for token in ("av01", "av1")):
        return "av1"
    if any(token in text for token in ("vp09", "vp9")):
        return "vp9"
    if any(token in text for token in ("vp08", "vp8")):
        return "vp8"
    return (value or "").lower()


def pretty_codec(codecs: str | None, media_type: str = "unknown") -> str | None:
    if not codecs:
        return None
    tokens = [token.strip() for token in codecs.split(",") if token.strip()]
    if media_type == "video":
        preferred = next(
            (
                token
                for token in tokens
                if token.lower().startswith(_VIDEO_CODEC_PREFIXES)
                or _compact_codec(token).startswith(("h264", "h265", "h266", "vvc"))
            ),
            tokens[0],
        )
    elif media_type == "audio":
        preferred = next(
            (
                token
                for token in tokens
                if token.lower().startswith(("mp4a", "aac", "ac-3", "ac-4", "ac4", "ec-3", "ec3", "dts", "opus", "flac"))
            ),
            tokens[0],
        )
    else:
        preferred = tokens[0]

    lowered = preferred.lower()
    compact = _compact_codec(lowered)
    if compact.startswith(("avc", "h264", "x264", "dva1", "dvav")):
        return "H.264"
    if compact.startswith(("hvc1", "hev1", "hevc", "h265", "x265", "dvh1", "dvhe")):
        return "H.265"
    if compact.startswith(("av01", "av1")):
        return "AV1"
    if compact.startswith(("vp09", "vp9")):
        return "VP9"
    if compact.startswith(("vp08", "vp8")):
        return "VP8"
    if compact.startswith(("vvc1", "vvi1", "h266", "vvc")):
        return "H.266"
    if lowered.startswith(("mp4v", "mpeg4", "mpeg-4")):
        return "MPEG-4"
    if lowered.startswith(("mp2v", "mpeg2", "mpeg-2")):
        return "MPEG-2"
    if lowered.startswith(("h263", "s263")):
        return "H.263"
    if lowered.startswith("theora"):
        return "Theora"
    if lowered.startswith(("mjpg", "jpeg", "jpeg2000", "mjpeg")):
        return "MJPEG"
    if lowered.startswith(("prores", "apch", "apcn", "apcs", "apco", "ap4h", "ap4x")):
        return "ProRes"
    if lowered.startswith(("he-aac", "heaac", "aac-he")):
        return "HE-AAC"
    if lowered.startswith(("mp4a", "aac", "aacl", "aach", "aace")):
        return "AAC"
    if lowered.startswith(("av3a", "avsa", "audio-vivid", "audiovivid")):
        return "Audio Vivid"
    if lowered.startswith("ac-3") or lowered.startswith("dac3"):
        return "AC-3"
    if lowered.startswith(("ac-4", "ac4", "dac4")):
        return "AC-4"
    if lowered.startswith(("ddplus", "ddp", "eac3", "e-ac-3")) or "ddplus" in lowered:
        return "E-AC-3 Atmos" if "atmos" in lowered or "joc" in lowered else "E-AC-3"
    if lowered.startswith(("ec-3", "ec3")) or lowered.startswith("dec3"):
        return "E-AC-3 Atmos" if "atmos" in lowered or "joc" in lowered else "E-AC-3"
    if lowered.startswith(("dtsx", "dts-x", "dts:x")):
        return "DTS:X"
    if lowered.startswith(("dtsh", "dtsl", "dtse")):
        return "DTS-HD"
    if lowered.startswith(("dtsc", "dts")):
        return "DTS"
    if lowered.startswith("opus"):
        return "Opus"
    if lowered.startswith(("wvtt", "vtt", "webvtt")):
        return "WebVTT"
    if lowered.startswith(("stpp", "ttml", "dfxp")):
        return "TTML"
    return preferred


def ceil_div_duration(total_seconds: float | None, duration_ticks: int, timescale: int) -> int:
    if not total_seconds or duration_ticks <= 0 or timescale <= 0:
        return 0
    return int(math.ceil(total_seconds * timescale / duration_ticks))


def unique_path(path: str | Path) -> Path:
    target = bounded_path(path)
    if not target.exists():
        return target
    for index in range(1, 10000):
        candidate = bounded_path(target.with_name(f"{target.stem}_{index}{target.suffix}"))
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find an unused output path for {target}")


__all__ = [
    "WINDOWS_INTERMEDIATE_PATH_LIMIT",
    "WINDOWS_PATH_LIMIT",
    "bounded_path",
    "bounded_child_path",
    "safe_temp_root",
    "windows_long_path",
    "unique_path",
]

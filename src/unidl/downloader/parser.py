from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

from .detect import DIRECT_EXTENSIONS, guess_kind
from .loader import LoadError, content_length, head_url, load_text
from .models import StreamInfo
from .parsers import parse_dash, parse_direct, parse_hls, parse_ism, parse_json
from .utils import is_url

MAX_MANIFEST_SNIFF_BYTES = 8 * 1024 * 1024


def parse_source(
    source: str,
    headers: dict[str, str] | None = None,
    probe_direct: bool = True,
    fetch_child_playlists: bool = True,
    base_url: str | None = None,
) -> list[StreamInfo]:
    kind = guess_kind(source)
    if kind == "direct":
        resource = _load_direct_manifest_candidate(source, headers=headers)
        if resource:
            child_headers = _headers_with_resource_cookies(headers, resource.headers)
            return _parse_manifest_resource(resource, child_headers, source, probe_direct, fetch_child_playlists, base_url)
        return parse_direct(source, headers=headers, probe=probe_direct)

    resource = _load_manifest(source, kind, headers=headers)
    child_headers = _headers_with_resource_cookies(headers, resource.headers)
    return _parse_manifest_resource(resource, child_headers, source, probe_direct, fetch_child_playlists, base_url)


def _parse_manifest_resource(
    resource,
    headers: dict[str, str] | None,
    source: str,
    probe_direct: bool,
    fetch_child_playlists: bool,
    base_url: str | None,
) -> list[StreamInfo]:
    kind = guess_kind(resource.uri, resource.text)

    parse_uri = base_url or resource.uri
    if kind == "hls":
        return parse_hls(parse_uri, resource.text or "", headers=headers, fetch_child_playlists=fetch_child_playlists)
    if kind == "dash":
        try:
            return parse_dash(parse_uri, resource.text or "")
        except ET.ParseError as exc:
            raise LoadError(f"Failed to parse DASH manifest: {exc}") from exc
    if kind == "ism":
        return parse_ism(parse_uri, resource.text or "")
    if kind == "json":
        return parse_json(parse_uri, resource.text or "", headers=headers)
    return parse_direct(source, headers=headers, probe=probe_direct)


def _load_direct_manifest_candidate(source: str, headers: dict[str, str] | None = None):
    if not is_url(source) or _has_direct_media_extension(source):
        return None

    response_headers = head_url(source, headers=headers)
    content_type = _header_value(response_headers, "content-type")
    should_sniff = _looks_like_manifest_content_type(content_type) or _looks_like_manifest_url(source)
    if not should_sniff:
        return None

    size = content_length(response_headers)
    if size and size > MAX_MANIFEST_SNIFF_BYTES and not _looks_like_manifest_content_type(content_type):
        return None

    try:
        resource = load_text(source, headers=headers)
    except LoadError:
        if _looks_like_manifest_content_type(content_type):
            raise
        return None

    sniffed_kind = guess_kind(resource.uri, resource.text)
    if sniffed_kind == "direct":
        return None
    return resource


def _has_direct_media_extension(source: str) -> bool:
    path = urlparse(source).path or source
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix in DIRECT_EXTENSIONS


def _looks_like_manifest_content_type(content_type: str | None) -> bool:
    normalized = (content_type or "").lower().split(";", 1)[0].strip()
    if not normalized:
        return False
    return any(
        token in normalized
        for token in (
            "dash+xml",
            "mpegurl",
            "vnd.apple.mpegurl",
            "x-mpegurl",
            "mpd",
            "xml",
            "json",
            "smooth",
        )
    )


def _looks_like_manifest_url(source: str) -> bool:
    path = (urlparse(source).path or source).lower()
    name = Path(path).name
    if name.endswith(("-dash", "_dash", "-mpd", "_mpd", "-manifest", "_manifest", "-playlist", "_playlist")):
        return True
    return any(token in path for token in ("/manifest", "/playlist", "/master", ".mpd", ".m3u8", ".ism/", ".isml/"))


def _header_value(headers: dict[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def _load_manifest(source: str, kind: str, headers: dict[str, str] | None = None):
    return load_text(source, headers=headers)


def _headers_with_resource_cookies(headers: dict[str, str] | None, resource_headers: dict[str, str] | None) -> dict[str, str] | None:
    cookie = _cookie_header_from_response(resource_headers)
    if not cookie:
        return headers
    if headers is None:
        return {"Cookie": cookie}
    existing = next((value for name, value in headers.items() if name.lower() == "cookie"), "")
    merged = _merge_cookie_headers(existing, cookie)
    if merged:
        for name in list(headers):
            if name.lower() == "cookie" and name != "Cookie":
                headers.pop(name, None)
        headers["Cookie"] = merged
    return headers


def _cookie_header_from_response(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    value = next((header_value for name, header_value in headers.items() if name.lower() == "set-cookie"), None)
    if not value:
        return None
    cookies: list[str] = []
    for line in str(value).splitlines():
        cookie = line.split(";", 1)[0].strip()
        if "=" in cookie:
            cookies.append(cookie)
    return "; ".join(cookies) or None


def _merge_cookie_headers(existing: str | None, incoming: str | None) -> str:
    merged: dict[str, str] = {}
    for source in [existing or "", incoming or ""]:
        for part in source.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            merged[name.strip()] = value.strip()
    return "; ".join(f"{name}={value}" for name, value in merged.items())

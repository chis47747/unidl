from __future__ import annotations

import gzip
import http.client
import shutil
import subprocess  # noqa: F401 - retained as the module's patch seam for hosts/tests
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from .embedding import current_download_runtime, managed_run
from .http_client import HttpClientError, get_global_http_client
from .live_rules import is_yangshipin_catchup_cdn_url
from .utils import is_file_url, is_url, source_path

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(slots=True)
class Resource:
    source: str
    final_url: str
    text: str | None = None
    bytes_data: bytes | None = None
    headers: dict[str, str] | None = None

    @property
    def uri(self) -> str:
        return self.final_url or self.source

    @property
    def is_remote(self) -> bool:
        return is_url(self.uri)


class LoadError(RuntimeError):
    pass


def _runtime_sleep(seconds: float) -> None:
    """Wake retry backoff when an embedded delivery is shutting down."""
    runtime = current_download_runtime()
    if runtime is None:
        time.sleep(max(0.0, float(seconds)))
    elif runtime.wait(seconds):
        runtime.checkpoint()


def normalize_headers(headers: list[str] | None = None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for header in headers or []:
        if ":" not in header:
            raise ValueError(f"Invalid header {header!r}; expected 'Name: value'.")
        name, value = header.split(":", 1)
        normalized[name.strip()] = value.strip()
    return normalized


def load_text(source: str, headers: dict[str, str] | None = None, timeout: float = 20, retries: int = 3) -> Resource:
    if is_url(source):
        catchup = is_yangshipin_catchup_cdn_url(source)
        if catchup:
            try:
                return _load_text_with_curl(source, headers=headers, timeout=timeout, retries=retries)
            except (OSError, HttpClientError) as exc:
                # Do not send this CDN back through the slower generic client;
                # it is the source of the long hangs this transport avoids.
                raise LoadError(f"Failed to fetch Yangshipin catch-up manifest: {exc}") from exc
        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                with get_global_http_client().request("GET", source, headers=_request_headers(headers), timeout=timeout) as response:
                    data = response.read()
                    data = _decode_content(data, response.get_header("Content-Encoding"))
                    response_headers = dict(response.headers)
                    charset = _charset_from_content_type(response.get_header("Content-Type")) or "utf-8"
                    text = data.decode(charset, errors="replace")
                    return Resource(source=source, final_url=response.url, text=text, bytes_data=data, headers=response_headers)
            except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException, HttpClientError) as exc:
                last_error = exc
                if attempt + 1 < retries:
                    _runtime_sleep(min(1 + attempt, 3))
        if _should_retry_youku_manifest_with_curl(source, last_error):
            try:
                return _load_text_with_curl(
                    source,
                    headers=headers,
                    timeout=timeout,
                    retries=retries,
                )
            except (OSError, HttpClientError):
                pass
        raise LoadError(f"Failed to fetch {source}: {last_error}") from last_error

    path = source_path(source)
    if not path.exists():
        raise LoadError(f"File not found: {path}")
    data = path.read_bytes()
    text = data.decode("utf-8-sig", errors="replace")
    return Resource(source=source, final_url=str(path.resolve()), text=text, bytes_data=data, headers={})


def load_bytes(source: str, headers: dict[str, str] | None = None, timeout: float = 20, retries: int = 3) -> Resource:
    if is_url(source):
        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                with get_global_http_client().request("GET", source, headers=_request_headers(headers), timeout=timeout) as response:
                    data = response.read()
                    data = _decode_content(data, response.get_header("Content-Encoding"))
                    return Resource(
                        source=source,
                        final_url=response.url,
                        bytes_data=data,
                        headers=dict(response.headers),
                    )
            except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException, HttpClientError) as exc:
                last_error = exc
                if attempt + 1 < retries:
                    _runtime_sleep(min(1 + attempt, 3))
        raise LoadError(f"Failed to fetch {source}: {last_error}") from last_error

    path = source_path(source)
    if not path.exists():
        raise LoadError(f"File not found: {path}")
    data = path.read_bytes()
    return Resource(source=source, final_url=str(path.resolve()), bytes_data=data, headers={})


def head_url(source: str, headers: dict[str, str] | None = None, timeout: float = 15) -> dict[str, str]:
    if not is_url(source):
        return {}
    try:
        with get_global_http_client().request("HEAD", source, headers=_request_headers(headers), timeout=timeout) as response:
            result = dict(response.headers)
            result["Final-Url"] = response.url
            return result
    except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException, HttpClientError):
        range_headers = _request_headers(headers)
        range_headers["Range"] = "bytes=0-0"
        try:
            with get_global_http_client().request("GET", source, headers=range_headers, timeout=timeout) as response:
                result = dict(response.headers)
                result["Final-Url"] = response.url
                return result
        except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException, HttpClientError):
            return {}


def local_file_size(source: str) -> int | None:
    if is_url(source):
        return None
    if is_file_url(source) or Path(source).expanduser().exists():
        path = source_path(source)
        return path.stat().st_size if path.exists() else None
    return None


def content_length(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    value = headers.get("Content-Length") or headers.get("content-length")
    if not value:
        content_range = headers.get("Content-Range") or headers.get("content-range")
        if content_range and "/" in content_range:
            value = content_range.rsplit("/", 1)[-1]
    try:
        return int(value) if value else None
    except ValueError:
        return None


def extension_from_source(source: str) -> str:
    parsed = urlparse(source)
    path = parsed.path if parsed.scheme else source
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix


def _request_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
    request_headers.update(headers or {})
    return request_headers


def _should_retry_youku_manifest_with_curl(
    source: str,
    error: BaseException | None,
) -> bool:
    host = (urlparse(source).hostname or "").lower()
    if host != "ott.cibntv.net" and not host.endswith(".ott.cibntv.net"):
        return False
    message = str(error or "").lower()
    return any(
        marker in message
        for marker in (
            "remote end closed",
            "remote disconnected",
            "connection reset",
            "unexpected eof",
        )
    )


def _load_text_with_curl(
    source: str,
    *,
    headers: dict[str, str] | None,
    timeout: float,
    retries: int,
) -> Resource:
    executable = shutil.which("curl")
    if not executable:
        raise OSError("curl is unavailable")
    catchup = is_yangshipin_catchup_cdn_url(source)
    with tempfile.TemporaryDirectory(prefix="unidown_manifest_") as temp_dir:
        root = Path(temp_dir)
        body_path = root / "body"
        args = [
            executable,
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            *(["--noproxy", "*"] if catchup else []),
            "--max-time",
            str(min(max(1, int(timeout)), 20) if catchup else max(1, int(timeout))),
            "--connect-timeout",
            str(min(max(1, int(timeout)), 10) if catchup else max(1, int(timeout))),
            "--retry",
            str(max(0, int(retries) - 1)),
            "--retry-delay",
            "1",
            "--retry-all-errors",
            "-o",
            str(body_path),
            "-w",
            "%{url_effective}",
        ]
        if catchup:
            args.extend(["--speed-limit", "16384", "--speed-time", "10"])
        for key, value in _request_headers(headers).items():
            args.extend(["-H", f"{key}: {value}"])
        args.append(source)
        result = managed_run(args, capture_output=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise HttpClientError(f"curl manifest request failed: {detail or f'exit {result.returncode}'}")
        data = body_path.read_bytes()
        charset = "utf-8"
        text = data.decode(charset, errors="replace")
        final_url = result.stdout.decode("utf-8", errors="replace").strip() or source
        return Resource(source=source, final_url=final_url, text=text, bytes_data=data, headers={})


def _decode_content(data: bytes, encoding: str | None) -> bytes:
    normalized = (encoding or "").lower()
    try:
        if "gzip" in normalized:
            return gzip.decompress(data)
        if "deflate" in normalized:
            return zlib.decompress(data)
    except (OSError, zlib.error):
        return data
    return data


def _charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.lower() == "charset" and value:
            return value.strip().strip('"')
    return None


def _parse_header_dump(text: str) -> dict[str, str]:
    blocks = [block for block in text.replace("\r\n", "\n").split("\n\n") if block.strip()]
    selected = ""
    for block in blocks:
        first = block.splitlines()[0] if block.splitlines() else ""
        if first.startswith("HTTP/") and not first.startswith("HTTP/1.1 100"):
            selected = block
    headers: dict[str, str] = {}
    set_cookies: list[str] = []
    for line in selected.splitlines()[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name.lower() == "set-cookie":
            set_cookies.append(value)
        else:
            headers[name] = value
    if set_cookies:
        headers["Set-Cookie"] = "\n".join(set_cookies)
    return headers

from __future__ import annotations

import http.client
import socket
import ssl
import struct
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit, urlunsplit

from .embedding import current_download_runtime

try:  # Optional fast path for environments that ship httpx with HTTP/2 support.
    import httpx as _httpx
except Exception:  # pragma: no cover - exercised in minimal dependency envs.
    _httpx = None


class HttpClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _Origin:
    scheme: str
    host: str
    port: int


class HttpResponse:
    def __init__(self, client: NativeHttpClient, origin: _Origin, connection: http.client.HTTPConnection, response: http.client.HTTPResponse, url: str):
        self._client = client
        self._origin = origin
        self._connection = connection
        self._response = response
        self.url = url
        self.status = response.status
        self.reason = response.reason
        headers: dict[str, str] = {}
        set_cookies: list[str] = []
        for key, value in response.getheaders():
            if key.lower() == "set-cookie":
                set_cookies.append(value)
            else:
                headers[key] = value
        if set_cookies:
            headers["Set-Cookie"] = "\n".join(set_cookies)
        self.headers = headers
        self._closed = False
        self._eof = response.length == 0 or response.status in {204, 304}
        self._reusable = (response.getheader("Connection") or "").lower() != "close"

    def __enter__(self) -> HttpResponse:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def get_header(self, name: str, default: str | None = None) -> str | None:
        value = self._response.getheader(name)
        return value if value is not None else default

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        data = self._response.read() if size < 0 else self._response.read(size)
        if not data or size < 0:
            self._eof = True
        return data

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._eof and self._reusable:
            try:
                self._response.read()
            except Exception:
                self._client._discard(self._connection)
                return
            self._client._release(self._origin, self._connection)
            return
        try:
            self._response.close()
        finally:
            self._client._discard(self._connection)


@dataclass(slots=True)
class Http2DownloadResponse:
    status: int
    reason: str
    headers: dict[str, str]
    url: str


_HTTPX_HTTP2_TLS = threading.local()
_HTTPX_CLIENTS_LOCK = threading.Lock()
_HTTPX_CLIENTS: set[object] = set()


class NativeHttpClient:
    def __init__(self, max_idle_per_origin: int = 32):
        self._max_idle_per_origin = max(1, max_idle_per_origin)
        self._pools: dict[_Origin, deque[http.client.HTTPConnection]] = defaultdict(deque)
        self._connections: set[http.client.HTTPConnection] = set()
        self._lock = threading.Lock()
        self._tls_context = ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        max_redirects: int = 8,
        body: bytes | None = None,
    ) -> HttpResponse:
        method = method.upper()
        current_url = url
        request_headers = dict(headers or {})
        current_body = body
        for _ in range(max(0, max_redirects) + 1):
            response = self._request_once(method, current_url, request_headers, timeout, current_body)
            if response.status not in {301, 302, 303, 307, 308}:
                if response.status >= 400:
                    detail = f"HTTP Error {response.status}: {response.reason}"
                    if response.status == 420 and (response.get_header("X-Netflix-Geo-Check") or "").lower() == "failed":
                        detail = "HTTP Error 420: Netflix geo check failed"
                    response.close()
                    raise HttpClientError(detail)
                return response
            location = response.get_header("Location")
            response.close()
            if not location:
                raise HttpClientError(f"Redirect response from {current_url} has no Location header.")
            current_url = urljoin(current_url, location)
            if response.status == 303:
                method = "GET"
                current_body = None
        raise HttpClientError(f"Too many redirects while fetching {url}.")

    def fetch_bytes(self, url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> bytes:
        with self.request("GET", url, headers=headers, timeout=timeout) as response:
            return response.read()

    def download_to_file(
        self,
        url: str,
        output: BinaryIO,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        chunk_size: int = 1024 * 1024,
        progress: Callable[[int], None] | None = None,
        limiter: object | None = None,
        low_speed_limit: int | None = None,
        low_speed_time: float | None = None,
        low_speed_min_bytes: int = 0,
        should_stop: Callable[[], bool] | None = None,
        allow_range_status_200: bool = False,
    ) -> tuple[int, HttpResponse]:
        with self.request("GET", url, headers=headers, timeout=timeout) as response:
            if (
                _header_value(headers or {}, "Range")
                and response.status != 206
                and not (allow_range_status_200 and response.status == 200)
            ):
                raise HttpClientError(f"Range request was not honored: HTTP {response.status} {response.reason}")
            total = 0
            low_speed = _LowSpeedWatch(low_speed_limit, low_speed_time, low_speed_min_bytes)
            while True:
                if should_stop and should_stop():
                    raise HttpClientError("download superseded by another attempt")
                low_speed.before_read()
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                low_speed.add(len(chunk))
                if limiter:
                    limiter.consume(len(chunk))
                output.write(chunk)
                total += len(chunk)
                if progress:
                    progress(len(chunk))
            return total, response

    def _request_once(self, method: str, url: str, headers: dict[str, str], timeout: float, body: bytes | None = None) -> HttpResponse:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HttpClientError(f"Unsupported URL for native HTTP backend: {url}")
        origin = _origin_from_url(url)
        connection = self._acquire(origin, timeout)
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
        except Exception as exc:
            self._discard(connection)
            raise HttpClientError(str(exc) or exc.__class__.__name__) from exc
        return HttpResponse(self, origin, connection, response, url)

    def _acquire(self, origin: _Origin, timeout: float) -> http.client.HTTPConnection:
        with self._lock:
            pool = self._pools.get(origin)
            while pool:
                connection = pool.pop()
                if not getattr(connection, "sock", None):
                    continue
                connection.timeout = timeout
                self._connections.add(connection)
                return connection
        if origin.scheme == "https":
            connection = _TunedHTTPSConnection(origin.host, origin.port, timeout=timeout, context=self._tls_context)
        else:
            connection = _TunedHTTPConnection(origin.host, origin.port, timeout=timeout)
        with self._lock:
            self._connections.add(connection)
        return connection

    def _release(self, origin: _Origin, connection: http.client.HTTPConnection) -> None:
        if not getattr(connection, "sock", None):
            return
        discard = False
        with self._lock:
            pool = self._pools[origin]
            if len(pool) >= self._max_idle_per_origin:
                discard = True
            else:
                pool.append(connection)
        if discard:
            self._discard(connection)

    def _discard(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            self._connections.discard(connection)
        try:
            connection.close()
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
            connections = list(self._connections)
            self._connections.clear()
        for pool in pools:
            while pool:
                self._discard(pool.pop())
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass


def httpx_http2_available() -> bool:
    return _httpx is not None


def close_http2_clients() -> None:
    """Close HTTP/2 clients owned by downloader worker threads."""
    with _HTTPX_CLIENTS_LOCK:
        clients = list(_HTTPX_CLIENTS)
        _HTTPX_CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


def _get_httpx_http2_client():
    if _httpx is None:
        raise HttpClientError("httpx with HTTP/2 support is not available.")
    client = getattr(_HTTPX_HTTP2_TLS, "client", None)
    if client is None or bool(getattr(client, "is_closed", False)):
        client = _httpx.Client(
            http2=True,
            follow_redirects=True,
            limits=_httpx.Limits(max_connections=64, max_keepalive_connections=16),
            timeout=None,
            trust_env=False,
        )
        _HTTPX_HTTP2_TLS.client = client
        with _HTTPX_CLIENTS_LOCK:
            _HTTPX_CLIENTS.add(client)
    return client


def _httpx_safe_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    connection_headers = {
        "connection",
        "http2-settings",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in connection_headers
    }


def _httpx_download_to_file(
    url: str,
    output: BinaryIO,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    chunk_size: int = 256 * 1024,
    progress: Callable[[int], None] | None = None,
    limiter: object | None = None,
    low_speed_limit: int | None = None,
    low_speed_time: float | None = None,
    low_speed_min_bytes: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[int, Http2DownloadResponse]:
    client = _get_httpx_http2_client()
    request_headers = _httpx_safe_headers(headers)
    request_timeout = _httpx.Timeout(timeout, connect=timeout, read=timeout, write=timeout, pool=timeout)
    low_speed = _LowSpeedWatch(low_speed_limit, low_speed_time, low_speed_min_bytes)
    try:
        with client.stream("GET", url, headers=request_headers, timeout=request_timeout) as response:
            if _header_value(headers or {}, "Range") and response.status_code != 206:
                raise HttpClientError(f"Range request was not honored: HTTP {response.status_code} {response.reason_phrase}")
            if response.status_code >= 400:
                raise HttpClientError(f"HTTP Error {response.status_code}: {response.reason_phrase}")
            total = 0
            chunks = response.iter_bytes(chunk_size=chunk_size)
            while True:
                if should_stop and should_stop():
                    raise HttpClientError("download superseded by another attempt")
                low_speed.before_read()
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                if not chunk:
                    continue
                low_speed.add(len(chunk))
                if limiter:
                    limiter.consume(len(chunk))
                output.write(chunk)
                total += len(chunk)
                if progress:
                    progress(len(chunk))
            response_headers = dict(response.headers)
            http_version = response.extensions.get("http_version")
            if isinstance(http_version, bytes):
                response_headers[":http-version"] = http_version.decode("ascii", "ignore")
            elif http_version:
                response_headers[":http-version"] = str(http_version)
            return total, Http2DownloadResponse(
                status=response.status_code,
                reason=response.reason_phrase,
                headers=response_headers,
                url=str(response.url),
            )
    except HttpClientError:
        raise
    except TimeoutError:
        raise
    except Exception as exc:
        if _httpx is not None and isinstance(exc, _httpx.HTTPError):
            raise HttpClientError(str(exc) or exc.__class__.__name__) from exc
        raise


def http2_download_to_file(
    url: str,
    output: BinaryIO,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    chunk_size: int = 256 * 1024,
    progress: Callable[[int], None] | None = None,
    limiter: object | None = None,
    low_speed_limit: int | None = None,
    low_speed_time: float | None = None,
    low_speed_min_bytes: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[int, Http2DownloadResponse]:
    if _httpx is not None:
        return _httpx_download_to_file(
            url,
            output,
            headers=headers,
            timeout=timeout,
            chunk_size=chunk_size,
            progress=progress,
            limiter=limiter,
            low_speed_limit=low_speed_limit,
            low_speed_time=low_speed_time,
            low_speed_min_bytes=low_speed_min_bytes,
            should_stop=should_stop,
        )
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HttpClientError(f"Unsupported URL for native HTTP/2 backend: {url}")
    port = parsed.port or 443
    context = ssl.create_default_context()
    context.set_alpn_protocols(["h2"])
    raw_sock = _create_connection((parsed.hostname, port), timeout)
    runtime = current_download_runtime()
    socket_token = runtime.register_closer(raw_sock.close) if runtime else None
    tls_sock: ssl.SSLSocket | None = None
    try:
        tls_sock = context.wrap_socket(raw_sock, server_hostname=parsed.hostname)
        tls_sock.settimeout(timeout)
        if tls_sock.selected_alpn_protocol() != "h2":
            raise HttpClientError("HTTP/2 was not negotiated by the remote host.")
        stream_id = 1
        _h2_write_preface(tls_sock)
        _h2_write_frame(tls_sock, _h2_settings_payload(), frame_type=0x4, flags=0, stream_id=0)
        _h2_write_window_update(tls_sock, 0, 16 * 1024 * 1024)
        header_block = _hpack_encode_request_headers(parsed, headers or {})
        _h2_write_frame(tls_sock, header_block, frame_type=0x1, flags=0x5, stream_id=stream_id)

        response_headers: dict[str, str] = {}
        status = 0
        total = 0
        low_speed = _LowSpeedWatch(low_speed_limit, low_speed_time, low_speed_min_bytes)
        pending_header_blocks: dict[int, bytearray] = {}
        while True:
            frame_type, flags, frame_stream_id, payload = _h2_read_frame(tls_sock)
            if frame_type == 0x4:  # SETTINGS
                if not flags & 0x1:
                    _h2_write_frame(tls_sock, b"", frame_type=0x4, flags=0x1, stream_id=0)
                continue
            if frame_type == 0x6:  # PING
                if not flags & 0x1 and len(payload) == 8:
                    _h2_write_frame(tls_sock, payload, frame_type=0x6, flags=0x1, stream_id=0)
                continue
            if frame_type == 0x7:  # GOAWAY
                raise HttpClientError("HTTP/2 GOAWAY received while downloading.")
            if frame_type == 0x3 and frame_stream_id == stream_id:  # RST_STREAM
                code = struct.unpack("!I", payload[:4] or b"\0\0\0\0")[0]
                raise HttpClientError(f"HTTP/2 stream reset: {code}")
            if frame_stream_id != stream_id:
                continue
            if frame_type == 0x1:  # HEADERS
                block = pending_header_blocks.setdefault(frame_stream_id, bytearray())
                block.extend(_h2_header_payload(payload, flags))
                if flags & 0x4:
                    response_headers.update(_hpack_decode_headers(bytes(block)))
                    pending_header_blocks.pop(frame_stream_id, None)
                    status = _status_from_headers(response_headers)
                if flags & 0x1:
                    break
                continue
            if frame_type == 0x9:  # CONTINUATION
                block = pending_header_blocks.setdefault(frame_stream_id, bytearray())
                block.extend(payload)
                if flags & 0x4:
                    response_headers.update(_hpack_decode_headers(bytes(block)))
                    pending_header_blocks.pop(frame_stream_id, None)
                    status = _status_from_headers(response_headers)
                continue
            if frame_type != 0x0:  # DATA
                continue
            data = _h2_data_payload(payload, flags)
            if data:
                if should_stop and should_stop():
                    raise HttpClientError("download superseded by another attempt")
                low_speed.before_read()
                low_speed.add(len(data))
                if limiter:
                    limiter.consume(len(data))
                output.write(data)
                total += len(data)
                if progress:
                    progress(len(data))
                _h2_write_window_update(tls_sock, 0, len(data))
                _h2_write_window_update(tls_sock, stream_id, len(data))
            if flags & 0x1:
                break
        status = status or _status_from_headers(response_headers)
        if status >= 400 or not status:
            raise HttpClientError(f"HTTP/2 Error {status or 'unknown'}")
        return total, Http2DownloadResponse(status=status, reason=str(status), headers=response_headers, url=url)
    finally:
        if runtime:
            runtime.unregister_closer(socket_token)
        try:
            if tls_sock is not None:
                tls_sock.close()
            else:
                raw_sock.close()
        except OSError:
            pass


def _origin_from_url(url: str) -> _Origin:
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise HttpClientError(f"Invalid URL: {url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _Origin(parsed.scheme, parsed.hostname, port)


def _header_value(headers: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


class _LowSpeedWatch:
    def __init__(self, limit: int | None, seconds: float | None, min_bytes: int = 0):
        self.limit = int(limit or 0)
        self.seconds = float(seconds or 0)
        self.min_bytes = max(0, int(min_bytes or 0))
        self.window_start: float | None = None
        self.window_bytes = 0
        self.total_bytes = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.seconds > 0

    def before_read(self) -> None:
        if self.enabled and self.window_start is None:
            self.window_start = time.monotonic()

    def add(self, size: int) -> None:
        if not self.enabled or size <= 0:
            return
        if self.window_start is None:
            self.window_start = time.monotonic()
        self.window_bytes += size
        self.total_bytes += size
        elapsed = time.monotonic() - self.window_start
        if elapsed < self.seconds:
            return
        speed = self.window_bytes / max(elapsed, 0.001)
        if self.total_bytes >= self.min_bytes and speed < self.limit:
            raise TimeoutError(f"low speed timeout: {speed:.0f} B/s below {self.limit} B/s for {elapsed:.1f}s")
        self.window_start = time.monotonic()
        self.window_bytes = 0


_H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_H2_INITIAL_WINDOW_SIZE = 16 * 1024 * 1024
_H2_MAX_FRAME_SIZE = 16 * 1024


_HPACK_STATIC_TABLE: list[tuple[str, str]] = [
    ("", ""),
    (":authority", ""),
    (":method", "GET"),
    (":method", "POST"),
    (":path", "/"),
    (":path", "/index.html"),
    (":scheme", "http"),
    (":scheme", "https"),
    (":status", "200"),
    (":status", "204"),
    (":status", "206"),
    (":status", "304"),
    (":status", "400"),
    (":status", "404"),
    (":status", "500"),
    ("accept-charset", ""),
    ("accept-encoding", "gzip, deflate"),
    ("accept-language", ""),
    ("accept-ranges", ""),
    ("accept", ""),
    ("access-control-allow-origin", ""),
    ("age", ""),
    ("allow", ""),
    ("authorization", ""),
    ("cache-control", ""),
    ("content-disposition", ""),
    ("content-encoding", ""),
    ("content-language", ""),
    ("content-length", ""),
    ("content-location", ""),
    ("content-range", ""),
    ("content-type", ""),
    ("cookie", ""),
    ("date", ""),
    ("etag", ""),
    ("expect", ""),
    ("expires", ""),
    ("from", ""),
    ("host", ""),
    ("if-match", ""),
    ("if-modified-since", ""),
    ("if-none-match", ""),
    ("if-range", ""),
    ("if-unmodified-since", ""),
    ("last-modified", ""),
    ("link", ""),
    ("location", ""),
    ("max-forwards", ""),
    ("proxy-authenticate", ""),
    ("proxy-authorization", ""),
    ("range", ""),
    ("referer", ""),
    ("refresh", ""),
    ("retry-after", ""),
    ("server", ""),
    ("set-cookie", ""),
    ("strict-transport-security", ""),
    ("transfer-encoding", ""),
    ("user-agent", ""),
    ("vary", ""),
    ("via", ""),
    ("www-authenticate", ""),
]


def _h2_write_preface(sock: ssl.SSLSocket) -> None:
    sock.sendall(_H2_PREFACE)


def _h2_settings_payload() -> bytes:
    return struct.pack("!HIHI", 0x4, _H2_INITIAL_WINDOW_SIZE, 0x5, _H2_MAX_FRAME_SIZE)


def _h2_write_frame(sock: ssl.SSLSocket, payload: bytes, frame_type: int, flags: int, stream_id: int) -> None:
    length = len(payload)
    if length > 0xFFFFFF:
        raise HttpClientError("HTTP/2 frame payload is too large.")
    header = length.to_bytes(3, "big") + bytes([frame_type & 0xFF, flags & 0xFF]) + struct.pack("!I", stream_id & 0x7FFFFFFF)
    sock.sendall(header + payload)


def _h2_read_frame(sock: ssl.SSLSocket) -> tuple[int, int, int, bytes]:
    header = _read_exact(sock, 9)
    length = int.from_bytes(header[:3], "big")
    frame_type = header[3]
    flags = header[4]
    stream_id = struct.unpack("!I", header[5:9])[0] & 0x7FFFFFFF
    payload = _read_exact(sock, length) if length else b""
    return frame_type, flags, stream_id, payload


def _h2_write_window_update(sock: ssl.SSLSocket, stream_id: int, increment: int) -> None:
    if increment <= 0:
        return
    increment = min(increment, 0x7FFFFFFF)
    _h2_write_frame(sock, struct.pack("!I", increment), frame_type=0x8, flags=0, stream_id=stream_id)


def _h2_header_payload(payload: bytes, flags: int) -> bytes:
    if not flags & 0x8:
        return payload
    if not payload:
        return b""
    pad_length = payload[0]
    end = max(1, len(payload) - pad_length)
    return payload[1:end]


def _h2_data_payload(payload: bytes, flags: int) -> bytes:
    if not flags & 0x8:
        return payload
    if not payload:
        return b""
    pad_length = payload[0]
    end = max(1, len(payload) - pad_length)
    return payload[1:end]


def _read_exact(sock: ssl.SSLSocket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise HttpClientError("Unexpected EOF while reading HTTP/2 frame.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _hpack_encode_request_headers(parsed, headers: dict[str, str]) -> bytes:
    authority = parsed.netloc
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    ordered: list[tuple[str, str]] = [
        (":method", "GET"),
        (":scheme", "https"),
        (":authority", authority),
        (":path", path),
    ]
    skipped = {"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "http2-settings"}
    for name, value in headers.items():
        lower = name.strip().lower()
        if not lower or lower in skipped:
            continue
        ordered.append((lower, str(value)))
    block = bytearray()
    for name, value in ordered:
        block.extend(_hpack_literal_without_indexing(name, value))
    return bytes(block)


def _hpack_literal_without_indexing(name: str, value: str) -> bytes:
    return b"\x00" + _hpack_string(name) + _hpack_string(value)


def _hpack_string(value: str) -> bytes:
    data = value.encode("utf-8")
    return _hpack_integer(len(data), 7, 0) + data


def _hpack_integer(value: int, prefix_bits: int, first_byte: int) -> bytes:
    max_prefix = (1 << prefix_bits) - 1
    if value < max_prefix:
        return bytes([first_byte | value])
    output = bytearray([first_byte | max_prefix])
    value -= max_prefix
    while value >= 128:
        output.append((value % 128) + 128)
        value //= 128
    output.append(value)
    return bytes(output)


def _hpack_decode_headers(block: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    index = 0
    while index < len(block):
        first = block[index]
        if first & 0x80:
            table_index, index = _hpack_decode_integer(block, index, 7)
            name, value = _hpack_static(table_index)
            if name:
                headers[name] = value
            continue
        if first & 0x40:
            name_index, index = _hpack_decode_integer(block, index, 6)
            name, index = _hpack_decode_name(block, index, name_index)
            value, index = _hpack_decode_string(block, index)
            if name:
                headers[name] = value
            continue
        if first & 0x20:
            _, index = _hpack_decode_integer(block, index, 5)
            continue
        name_index, index = _hpack_decode_integer(block, index, 4)
        name, index = _hpack_decode_name(block, index, name_index)
        value, index = _hpack_decode_string(block, index)
        if name:
            headers[name] = value
    return headers


def _hpack_decode_name(block: bytes, index: int, name_index: int) -> tuple[str, int]:
    if name_index:
        name, _value = _hpack_static(name_index)
        return name, index
    return _hpack_decode_string(block, index)


def _hpack_decode_integer(block: bytes, index: int, prefix_bits: int) -> tuple[int, int]:
    first = block[index]
    index += 1
    max_prefix = (1 << prefix_bits) - 1
    value = first & max_prefix
    if value < max_prefix:
        return value, index
    shift = 0
    while index < len(block):
        byte = block[index]
        index += 1
        value += (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return value, index


def _hpack_decode_string(block: bytes, index: int) -> tuple[str, int]:
    if index >= len(block):
        return "", index
    huffman = bool(block[index] & 0x80)
    length, index = _hpack_decode_integer(block, index, 7)
    data = block[index : index + length]
    index += length
    if huffman:
        return "", index
    return data.decode("utf-8", errors="replace"), index


def _hpack_static(index: int) -> tuple[str, str]:
    if 0 < index < len(_HPACK_STATIC_TABLE):
        return _HPACK_STATIC_TABLE[index]
    return "", ""


def _status_from_headers(headers: dict[str, str]) -> int:
    try:
        return int(headers.get(":status") or headers.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def _tune_socket(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


def _create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    host, port = address
    family = socket.AF_INET if _prefer_ipv4_host(host) else 0
    last_error: OSError | None = None
    for family_value, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
        sock = socket.socket(family_value, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            _tune_socket(sock)
            return sock
        except OSError as exc:
            last_error = exc
            try:
                sock.close()
            except OSError:
                pass
    if last_error:
        raise last_error
    raise OSError(f"getaddrinfo returns an empty list for {host}:{port}")


def _prefer_ipv4_host(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    return normalized.startswith("ipv6-") and normalized.endswith(".nflxvideo.net")


class _TunedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _create_connection((self.host, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _TunedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        self.sock = _create_connection((self.host, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


_GLOBAL_CLIENT = NativeHttpClient()


def get_global_http_client() -> NativeHttpClient:
    return _GLOBAL_CLIENT

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
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

from ..core.diagnostics import current_recorder
from ..core.network import NetworkCancellation, SocketLease, network_checkpoint
from .embedding import current_download_runtime, current_proxy_policy

try:  # Optional fast path for environments that ship httpx with HTTP/2 support.
    import httpx as _httpx
except Exception:  # pragma: no cover - exercised in minimal dependency envs.
    _httpx = None


class HttpClientError(RuntimeError):
    pass


def _encoded_http_url(url: str) -> str:
    """Encode raw request-target characters without rewriting signed queries."""
    # urlsplit silently strips CR/LF/TAB; reject them before parsing instead.
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise HttpClientError("HTTP URL contains a control character")
    parsed = urlsplit(url)
    # Preserve delimiters, literal plus signs and existing percent escapes.
    # parse_qs/urlencode would change duplicate parameters/order and signatures.
    safe = "/?:@!$&'()*+,;=%[]"
    return urlunsplit(
        parsed._replace(
            path=quote(parsed.path, safe=safe),
            query=quote(parsed.query, safe=safe),
        )
    )


@dataclass(frozen=True, slots=True)
class _Origin:
    scheme: str
    host: str
    port: int


_REDIRECT_HEADERS = frozenset({"accept", "accept-encoding", "accept-language", "user-agent", "range", "if-range"})


def redirect_headers(
    source: str, target: str, headers: dict[str, str], *, crossed_origin: bool = False,
) -> dict[str, str]:
    """Carry only portable headers if a redirect leaves the trusted origin."""
    if not crossed_origin and same_origin(source, target):
        return dict(headers)
    return {name: value for name, value in headers.items() if name.lower() in _REDIRECT_HEADERS}


def same_origin(source: str, target: str) -> bool:
    return _origin_from_url(source) == _origin_from_url(target)


def _request_proxy(url: str) -> str | None:
    explicit, use_system = current_proxy_policy()
    if explicit:
        return explicit
    if not use_system:
        return None
    parsed = urlsplit(url)
    proxies = getproxies()
    host = parsed.netloc.rsplit("@", 1)[-1]
    if not proxies or proxy_bypass(host):
        return None
    proxy = proxies.get(parsed.scheme) or proxies.get("all")
    return f"http://{proxy}" if proxy and "://" not in proxy else proxy


class HttpResponse:
    def __init__(self, client: NativeHttpClient, origin: _Origin, connection: http.client.HTTPConnection, response: http.client.HTTPResponse, url: str, sock: socket.socket):
        self._client = client
        self._origin = origin
        self._connection = connection
        self._response = response
        self._socket = sock
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
        self.crossed_origin = False
        self._closed = False
        self._eof = response.length == 0 or response.status in {204, 304}
        self._reusable = not response.will_close

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
        with self._client._interrupt.activate(), SocketLease(self._socket):
            data = self._response.read() if size < 0 else self._response.read(size)
            network_checkpoint()
        if not data or size < 0:
            self._eof = True
        return data

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._eof and self._reusable:
            try:
                with self._client._interrupt.activate(), SocketLease(self._socket):
                    self._response.read()
                    network_checkpoint()
            except Exception:
                self._client._discard(self._connection)
                return
            self._client._release(self._origin, self._connection)
            return
        try:
            self._response.close()
        finally:
            self._client._discard(self._connection)

class _InterruptibleStream:
    """Wrap httpcore's sync I/O, including the read waiting for headers."""

    def __init__(self, stream):
        self.stream = stream

    def read(self, *args, **kwargs):
        with SocketLease(self.get_extra_info("socket")):
            data = self.stream.read(*args, **kwargs)
            network_checkpoint()
            return data

    def write(self, *args, **kwargs):
        with SocketLease(self.get_extra_info("socket")):
            return self.stream.write(*args, **kwargs)

    def start_tls(self, *args, **kwargs):
        with SocketLease(self.get_extra_info("socket"), duplicate=True):
            return _InterruptibleStream(self.stream.start_tls(*args, **kwargs))

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.stream.close()


class _InterruptibleBackend:
    def __init__(self, backend):
        self.backend = backend

    def connect_tcp(self, *args, **kwargs):
        network_checkpoint()
        return _InterruptibleStream(self.backend.connect_tcp(*args, **kwargs))

    def connect_unix_socket(self, *args, **kwargs):
        network_checkpoint()
        return _InterruptibleStream(self.backend.connect_unix_socket(*args, **kwargs))

    def sleep(self, seconds):
        self.backend.sleep(seconds)


class _HttpxResponse:
    """Expose the same streaming interface as the direct HTTP transport."""

    def __init__(self, response, *, decode_content: bool, interrupt: NetworkCancellation):
        self._response = response
        self._interrupt = interrupt
        self._chunks = response.iter_bytes() if decode_content else response.iter_raw()
        self._buffer = bytearray()
        self.url = str(response.url)
        self.status = response.status_code
        self.reason = response.reason_phrase
        self.headers = {name.title(): value for name, value in response.headers.items()}
        self.crossed_origin = False
        self.headers[":http-version"] = response.http_version
        cookies = response.headers.get_list("set-cookie")
        if cookies:
            self.headers["Set-Cookie"] = "\n".join(cookies)

    def __enter__(self) -> _HttpxResponse:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def get_header(self, name: str, default: str | None = None) -> str | None:
        return self._response.headers.get(name, default)

    def read(self, size: int = -1) -> bytes:
        with self._interrupt.activate():
            network_checkpoint()
            return self._read(size)

    def _read(self, size: int) -> bytes:
        if self._response.is_closed and not self._buffer:
            return b""
        try:
            if size < 0:
                for chunk in self._chunks:
                    self._buffer.extend(chunk)
                size = len(self._buffer)
            else:
                while len(self._buffer) < size:
                    chunk = next(self._chunks, None)
                    if chunk is None:
                        break
                    self._buffer.extend(chunk)
        except _httpx.HTTPError as exc:
            raise HttpClientError(str(exc) or exc.__class__.__name__) from exc
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def close(self) -> None:
        self._response.close()


@dataclass(slots=True)
class Http2DownloadResponse:
    status: int
    reason: str
    headers: dict[str, str]
    url: str


class NativeHttpClient:
    def __init__(self, max_idle_per_origin: int = 32):
        self._max_idle_per_origin = max(1, max_idle_per_origin)
        self._pools: dict[_Origin, deque[http.client.HTTPConnection]] = defaultdict(deque)
        self._connections: set[http.client.HTTPConnection] = set()
        self._httpx_clients: dict[tuple[str | None, bool], _httpx.Client] = {}
        self._lock = threading.Lock()
        self._closing = False
        self._interrupt = NetworkCancellation()
        self._tls_context = ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        max_redirects: int = 8,
        body: bytes | None = None,
        *,
        http2: bool = False,
    ) -> HttpResponse | _HttpxResponse:
        method = method.upper()
        current_url = _encoded_http_url(url)
        request_headers = dict(headers or {})
        current_body = body
        crossed_origin = False
        for _ in range(max(0, max_redirects) + 1):
            recorder = current_recorder()
            if recorder is not None:
                recorder.record_request(method, current_url, request_headers, current_body)
            started = time.monotonic()
            try:
                response = self._request_once(method, current_url, request_headers, timeout, current_body, http2=http2)
            except Exception as exc:
                if recorder is not None:
                    recorder.record_response(
                        method,
                        current_url,
                        None,
                        request_body=current_body,
                        elapsed=time.monotonic() - started,
                        error=exc,
                    )
                raise
            if recorder is not None:
                recorder.record_response(
                    method,
                    current_url,
                    response,
                    request_body=current_body,
                    elapsed=time.monotonic() - started,
                )
            if response.status not in {301, 302, 303, 307, 308}:
                if response.status >= 400:
                    detail = f"HTTP Error {response.status}: {response.reason}"
                    if response.status == 420 and (response.get_header("X-Netflix-Geo-Check") or "").lower() == "failed":
                        detail = "HTTP Error 420: Netflix geo check failed"
                    response.close()
                    raise HttpClientError(detail)
                response.crossed_origin = crossed_origin
                return response
            location = response.get_header("Location")
            response.close()
            if not location:
                raise HttpClientError("Redirect response has no Location header.")
            next_url = urljoin(current_url, _encoded_http_url(location))
            if response.status == 303 and method != "HEAD":
                method = "GET"
                current_body = None
                request_headers = {
                    name: value for name, value in request_headers.items()
                    if name.lower() not in {"content-length", "content-type", "transfer-encoding"}
                }
            cross_origin = not same_origin(current_url, next_url)
            if current_body and cross_origin:
                raise HttpClientError("Refusing to redirect a request body to a different origin.")
            request_headers = redirect_headers(current_url, next_url, request_headers)
            crossed_origin |= cross_origin
            current_url = next_url
        raise HttpClientError("Too many HTTP redirects.")

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
        *,
        http2: bool = False,
    ) -> tuple[int, HttpResponse | _HttpxResponse]:
        request_options = {"http2": True} if http2 else {}
        with self.request("GET", url, headers=headers, timeout=timeout, **request_options) as response:
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

    def _request_once(
        self, method: str, url: str, headers: dict[str, str], timeout: float, body: bytes | None = None,
        *, http2: bool = False,
    ) -> HttpResponse | _HttpxResponse:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HttpClientError("Unsupported URL for native HTTP backend.")
        proxy = _request_proxy(url)
        if proxy or http2:
            return self._request_with_httpx(method, url, headers, timeout, body, proxy=proxy, http2=http2)
        origin = _origin_from_url(url)
        connection = self._acquire(origin, timeout)
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            with self._interrupt.activate():
                network_checkpoint()
                if connection.sock is None:
                    connection.connect()
                # getresponse() can detach a Connection: close socket, so keep
                # this reference independently of the connection pool.
                sock = connection.sock
                with SocketLease(sock):
                    connection.request(method, path, body=body, headers=headers)
                    response = connection.getresponse()
        except Exception as exc:
            self._discard(connection)
            raise HttpClientError(str(exc) or exc.__class__.__name__) from exc
        return HttpResponse(self, origin, connection, response, url, sock)

    def _request_with_httpx(self, method, url, headers, timeout, body, *, proxy, http2) -> _HttpxResponse:
        if _httpx is None:
            raise HttpClientError("The configured HTTP transport requires httpx.")
        try:
            with self._lock:
                if self._closing:
                    raise HttpClientError("HTTP client is closed.")
                route = (proxy, http2)
                client = self._httpx_clients.get(route)
                if client is None:
                    transport = _httpx.HTTPTransport(
                        proxy=proxy, http2=http2, trust_env=False,
                        limits=_httpx.Limits(max_connections=64, max_keepalive_connections=16),
                    )
                    # HTTPX currently has no public synchronous network-backend
                    # parameter. This is a pool owned by this client, not a
                    # process-wide patch; keep the normal transport if it changes.
                    pool = getattr(transport, "_pool", None)
                    backend = getattr(pool, "_network_backend", None)
                    if backend is not None:
                        pool._network_backend = _InterruptibleBackend(backend)
                    client = _httpx.Client(transport=transport, follow_redirects=False, trust_env=False)
                    self._httpx_clients[route] = client
            # Building a raw request keeps transport cookies and auth out of
            # requests made for another stream or to a redirect destination.
            request = _httpx.Request(
                method, url, headers=_httpx_safe_headers(headers) if http2 else headers, content=body,
                extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), timeout)},
            )
            with self._interrupt.activate():
                network_checkpoint()
                response = client.send(request, stream=True)
            return _HttpxResponse(response, decode_content=http2, interrupt=self._interrupt)
        except (ValueError, ImportError) as exc:
            raise HttpClientError("The configured HTTP transport is unavailable or invalid.") from exc
        except _httpx.HTTPError as exc:
            detail = exc.__class__.__name__ if proxy else (str(exc) or exc.__class__.__name__)
            raise HttpClientError(detail) from exc

    def _acquire(self, origin: _Origin, timeout: float) -> http.client.HTTPConnection:
        with self._lock:
            if self._closing:
                raise HttpClientError("HTTP client is closed.")
            pool = self._pools.get(origin)
            while pool:
                connection = pool.pop()
                if not getattr(connection, "sock", None):
                    continue
                connection.timeout = timeout
                try:
                    connection.sock.settimeout(timeout)
                except OSError:
                    # A pooled socket can be closed between requests. Do not
                    # reuse it, or fail an otherwise valid new request.
                    self._connections.discard(connection)
                    try:
                        connection.close()
                    except OSError:
                        pass
                    continue
                self._connections.add(connection)
                return connection
        if origin.scheme == "https":
            connection = _TunedHTTPSConnection(origin.host, origin.port, timeout=timeout, context=self._tls_context)
        else:
            connection = _TunedHTTPConnection(origin.host, origin.port, timeout=timeout)
        with self._lock:
            if self._closing:
                raise HttpClientError("HTTP client is closed.")
            self._connections.add(connection)
        return connection

    def _release(self, origin: _Origin, connection: http.client.HTTPConnection) -> None:
        if not getattr(connection, "sock", None):
            return
        discard = False
        with self._lock:
            pool = self._pools[origin]
            if self._closing or len(pool) >= self._max_idle_per_origin:
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
        self._interrupt.cancel()
        with self._lock:
            self._closing = True
            pools = list(self._pools.values())
            self._pools.clear()
            connections = list(self._connections)
            self._connections.clear()
            clients = list(self._httpx_clients.values())
            self._httpx_clients.clear()
        # HTTPResponse owns a buffered socket reader. Closing the connection's
        # socket alone may leave that reader's reference alive, then block in
        # response.close() waiting for its read lock. Interrupt reads first.
        for connection in connections:
            sock = getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        for pool in pools:
            while pool:
                self._discard(pool.pop())
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        for client in clients:
            try:
                client.close()
            except OSError:
                pass


def httpx_http2_available() -> bool:
    return _httpx is not None


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
    url = _encoded_http_url(url)
    if _httpx is not None:
        total, response = get_global_http_client().download_to_file(
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
            http2=True,
        )
        return total, Http2DownloadResponse(response.status, response.reason, response.headers, response.url)
    if _request_proxy(url):
        raise HttpClientError("HTTP/2 through a proxy requires httpx.")
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
            with SocketLease(sock):
                sock.connect(sockaddr)
                network_checkpoint()
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
        with SocketLease(self.sock, duplicate=True):
            self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


_GLOBAL_CLIENT = NativeHttpClient()


def get_global_http_client() -> NativeHttpClient:
    """Return the active download's client, or the shared client for other callers."""
    runtime = current_download_runtime()
    return runtime.resource(NativeHttpClient) if runtime is not None else _GLOBAL_CLIENT

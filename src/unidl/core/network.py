"""Cooperative network cancellation without closing another session's sockets."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

import requests
from requests.adapters import HTTPAdapter, TimeoutSauce

_active: ContextVar[tuple[NetworkCancellation, ...]] = ContextVar("unidl_network_cancellation", default=())
_cleanup_deadline: ContextVar[float | None] = ContextVar("unidl_cleanup_deadline", default=None)


class NetworkStopped(OSError):
    """The owner interrupted its network operation."""


class NetworkCancellation:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sockets: dict[object, socket.socket] = {}

    @contextmanager
    def activate(self):
        token = _active.set((*_active.get(), self))
        try:
            yield
        finally:
            _active.reset(token)

    @contextmanager
    def suspend(self):
        token = _active.set(tuple(scope for scope in _active.get() if scope is not self))
        try:
            yield
        finally:
            _active.reset(token)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def cancel(self) -> None:
        # shutdown(), not close(): a buffered HTTP response may still own the
        # socket. Also don't call response.close() while a read holds its lock.
        with self._lock:
            self._stop.set()
            sockets = tuple(self._sockets.values())
            self._sockets.clear()
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _attach(self, key, sock) -> None:
        with self._lock:
            if self.stopped:
                raise NetworkStopped("network operation canceled")
            self._sockets[key] = sock

    def _detach(self, key) -> None:
        with self._lock:
            self._sockets.pop(key, None)


def network_checkpoint() -> None:
    if any(scope.stopped for scope in _active.get()):
        raise NetworkStopped("network operation canceled")


@contextmanager
def playback_cleanup_scope(*, timeout: float = 2.0):
    """Permit only bounded cleanup requests after the owning job is canceled."""
    cleanup = NetworkCancellation()
    deadline = _cleanup_deadline.set(time.monotonic() + timeout)
    scopes = _active.set((cleanup,))
    timer = threading.Timer(timeout, cleanup.cancel)
    timer.daemon = True
    try:
        timer.start()
        yield
    finally:
        timer.cancel()
        cleanup.cancel()
        _active.reset(scopes)
        _cleanup_deadline.reset(deadline)


def _cleanup_timeout(timeout, remaining: float):
    if remaining <= 0:
        raise NetworkStopped("playback cleanup timed out")
    if isinstance(timeout, TimeoutSauce):
        timeout = timeout.clone()
        timeout.start_connect()
        connect, read = timeout.connect_timeout, timeout.read_timeout
        if isinstance(timeout.total, (int, float)):
            remaining = min(remaining, timeout.total)
    elif isinstance(timeout, tuple):
        connect, read = timeout
    else:
        connect = read = timeout

    def bounded(value):
        return min(remaining, value) if isinstance(value, (int, float)) else remaining

    return TimeoutSauce(connect=bounded(connect), read=bounded(read), total=remaining)


def network_retry_delay(seconds: float) -> None:
    """Wait between requests, including preparation before a delivery exists."""
    scopes = _active.get()
    if not scopes:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        network_checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        scopes[-1]._stop.wait(min(remaining, 0.05))


class SocketLease:
    """Keep the actual socket interruptible until the blocking I/O is done."""

    def __init__(self, sock, *, duplicate: bool = False):
        self.scopes = _active.get()
        # TLS can take ownership of the socket descriptor during a handshake.
        # A duplicate can still interrupt that same TCP connection.
        self.owned = None
        if duplicate and self.scopes and sock is not None:
            try:
                self.owned = sock.dup()
            except (AttributeError, NotImplementedError):
                pass  # TLS-in-TLS wrappers can already own the descriptor.
        watched = self.owned if self.owned is not None else sock
        try:
            if watched is not None:
                for scope in self.scopes:
                    scope._attach(self, watched)
            network_checkpoint()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for scope in self.scopes:
            scope._detach(self)
        if self.owned is not None:
            self.owned.close()
            self.owned = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class _ConnectionMixin:
    """Lease a urllib3 connection through headers and streamed response body."""

    _network_lease = None
    _receiving_headers = False

    def _watch(self, sock, *, duplicate=False):
        self._unwatch()
        self._network_lease = SocketLease(sock, duplicate=duplicate)

    def _unwatch(self):
        if self._network_lease is not None:
            self._network_lease.close()
            self._network_lease = None

    def _new_conn(self):
        network_checkpoint()
        sock = super()._new_conn()
        try:
            self._watch(sock, duplicate=True)
        except BaseException:
            sock.close()
            raise
        return sock

    def request(self, *args, **kwargs):
        network_checkpoint()
        if self.sock is not None:
            # A reused TLS-in-TLS proxy stream may not expose dup(). It still
            # provides shutdown() through urllib3's socket wrapper.
            self._watch(self.sock)
        return super().request(*args, **kwargs)

    def getresponse(self):
        # HTTPConnection.getresponse() calls close() for Connection: close,
        # even though the response's buffered reader is still using the socket.
        self._receiving_headers = True
        try:
            return super().getresponse()
        finally:
            self._receiving_headers = False

    def close(self):
        try:
            super().close()
        finally:
            if not self._receiving_headers:
                self._unwatch()


class _PoolMixin:
    def _put_conn(self, conn):
        if conn is not None:
            conn._unwatch()
        return super()._put_conn(conn)


def _interruptible_pools(manager):
    if getattr(manager, "_unidl_interruptible", False):
        return manager
    manager.pool_classes_by_scheme = {
        scheme: type(
            pool.__name__, (_PoolMixin, pool),
            {"ConnectionCls": type(pool.ConnectionCls.__name__, (_ConnectionMixin, pool.ConnectionCls), {})},
        )
        for scheme, pool in manager.pool_classes_by_scheme.items()
    }
    manager._unidl_interruptible = True
    return manager


class _InterruptibleAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        _interruptible_pools(self.poolmanager)

    def proxy_manager_for(self, proxy, **kwargs):
        return _interruptible_pools(super().proxy_manager_for(proxy, **kwargs))

    def send(self, request, **kwargs):
        network_checkpoint()
        response = super().send(request, **kwargs)
        try:
            network_checkpoint()
        except NetworkStopped:
            response.close()
            raise
        return response


class NetworkSession(requests.Session):
    """A normal Requests session whose built-in adapters respect the active scope."""

    def __init__(self):
        super().__init__()
        for prefix in ("http://", "https://"):
            self.adapters[prefix].close()
            self.mount(prefix, _InterruptibleAdapter())

    def send(self, request, **kwargs):
        deadline = _cleanup_deadline.get()
        if deadline is not None:
            kwargs["timeout"] = _cleanup_timeout(kwargs.get("timeout"), deadline - time.monotonic())
        response = super().send(request, **kwargs)
        try:
            network_checkpoint()
        except NetworkStopped:
            response.close()
            raise
        return response


def network_get(url, **kwargs):
    """Apply Core's scoped transport while preserving Requests' GET API."""
    if not _active.get():
        return requests.get(url, **kwargs)
    with NetworkSession() as session:
        return session.get(url, **kwargs)

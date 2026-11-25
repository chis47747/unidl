"""Public callbacks for UniDL hosts driving the native downloader.

The standalone CLI owns its terminal and can ask the user for a rotated live
key.  An embedding application owns that terminal instead, so it supplies the
same answer through :class:`DownloadHooks` without patching CLI internals or
competing for stdin.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.error import HTTPError

from ..core.delivery import LiveSourceExpired, LiveSourceRenewal
from ..core.network import NetworkCancellation

if TYPE_CHECKING:
    from .models import SegmentInfo, StreamInfo

_Resource = TypeVar("_Resource")


class LiveRecordingStopped(Exception):
    """End capture at the last usable batch, without cancelling finalization."""


@dataclass(frozen=True, slots=True)
class LiveKeyRequest:
    """Context for a key first observed during an active live recording."""

    kid: str | None
    stream: StreamInfo
    segment: SegmentInfo | None
    reason: str
    require_kid: bool = False
    force: bool = False
    replace_existing: bool = False
    media_playlist: str = ""
    manifest_url: str = ""
    initialization: SegmentInfo | None = None


LiveKeyProvider = Callable[[LiveKeyRequest], str | None]


class DownloadCancelled(RuntimeError):
    """The embedding application requested an orderly download stop."""


class VodTransferAttempt:
    """State shared only by the native workers using one source generation."""

    def __init__(self, cache_id: str) -> None:
        self.cache_id = cache_id
        self.lock = threading.Lock()
        self.failure: tuple[int, str] | None = None
        self.completed: set[tuple[object, ...]] = set()

    def note_failure(self, status: int, phase: str) -> None:
        with self.lock:
            if self.failure is None:
                self.failure = (status, phase)

    def note_completed(self, position: tuple[object, ...]) -> None:
        with self.lock:
            self.completed.add(position)


class DownloadRuntime:
    """Own the resources which must die with one embedded download.

    Cooperative cancellation is still useful for preserving a clean partial
    download, but it cannot interrupt a socket read or a child process.  The
    runtime is the hard-stop side of that contract: it closes registered
    transports and terminates registered process groups immediately.
    """

    _current: contextvars.ContextVar[DownloadRuntime | None] = contextvars.ContextVar(
        "unidl_download_runtime",
        default=None,
    )

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._network = NetworkCancellation()
        self.live_authorization = NetworkCancellation()
        self.vod_transfer: VodTransferAttempt | None = None
        self._lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._shutdown_thread: threading.Thread | None = None
        self._processes: dict[int, tuple[subprocess.Popen, bool]] = {}
        self._closers: dict[int, Callable[[], None]] = {}
        self._next_closer = 0
        self._resources: dict[object, object] = {}
        self._capture: DownloadRuntime | None = None
        self._capture_started = False
        self._closed = False

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self) -> None:
        if self.stopped:
            raise DownloadCancelled("download stopped because UniDL is exiting")

    def wait(self, timeout: float) -> bool:
        """Wait interruptibly; return whether shutdown was requested."""
        return self._stop.wait(max(0.0, float(timeout)))

    @contextlib.contextmanager
    def bind(self):
        """Use the existing owner without closing it on context exit."""
        previous = self._current.get()
        token = self._current.set(self)
        try:
            # Preserve the session/job scopes but switch from capture to the
            # delivery's scope for callbacks that write completed batches.
            previous_scope = previous._network.suspend() if previous and previous is not self else contextlib.nullcontext()
            with previous_scope, self._network.activate():
                yield
        finally:
            self._current.reset(token)

    @contextlib.contextmanager
    def activate(self):
        """Bind one delivery to the calling context and its explicit workers."""
        if self._current.get() is self:
            yield
            return
        with self.bind():
            try:
                yield self
            finally:
                self.close()

    @contextlib.contextmanager
    def live_capture(self, stop_requested):
        """Keep capture I/O separate from the output and final mux resources."""
        child = DownloadRuntime()
        with self._lock:
            self._capture = child
            self._capture_started = True
        token = self.register_closer(child.shutdown)
        try:
            if stop_requested():
                child.request_shutdown()
            with child.activate():
                yield
        finally:
            with self._lock:
                self._capture = None
            self.unregister_closer(token)

    def interrupt_live_capture(self) -> bool:
        self.live_authorization.cancel()
        with self._lock:
            child = self._capture
            started = self._capture_started
        if child is not None:
            child.request_shutdown()
        return started

    @classmethod
    def current(cls) -> DownloadRuntime | None:
        return cls._current.get()

    def resource(self, factory: Callable[[], _Resource]) -> _Resource:
        """Get the task's instance of a closable transport, creating it once."""
        with self._lock:
            if self.stopped or self._closed:
                raise DownloadCancelled("download transport is closed")
            if factory in self._resources:
                return self._resources[factory]
            instance = factory()
            self.register_closer(instance.close)
            self._resources[factory] = instance
            return instance

    def register_process(self, process: subprocess.Popen, *, process_group: bool = False) -> subprocess.Popen:
        with self._lock:
            if self.stopped or self._closed:
                should_stop = True
            else:
                self._processes[id(process)] = (process, process_group)
                should_stop = False
        if should_stop:
            self._terminate_process(process, process_group)
            raise DownloadCancelled("download stopped because UniDL is exiting")
        return process

    def unregister_process(self, process: subprocess.Popen | None) -> None:
        if process is None:
            return
        with self._lock:
            self._processes.pop(id(process), None)

    def register_closer(self, closer: Callable[[], None]) -> int:
        with self._lock:
            self._next_closer += 1
            token = self._next_closer
            if not self.stopped and not self._closed:
                self._closers[token] = closer
                return token
        try:
            closer()
        except Exception:
            pass
        return token

    def unregister_closer(self, token: int | None) -> None:
        if token is None:
            return
        with self._lock:
            self._closers.pop(token, None)

    def request_shutdown(self, *, grace: float = 0.15) -> None:
        """Signal cancellation immediately without closing resources on the UI.

        Closing a transport may wait for a reader, whose progress/exit callback
        needs that same UI thread. One owned cleanup thread breaks that cycle;
        the delivery owner joins it in close() before releasing this runtime.
        """
        self._stop.set()
        self._network.cancel()
        # A capture can own curl as well as sockets. Don't leave its children
        # queued behind an unrelated slow delivery closer on a hard quit.
        self.interrupt_live_capture()
        with self._lock:
            if self._shutdown_thread is not None:
                return
            self._shutdown_thread = threading.Thread(
                target=self._close_resources,
                kwargs={"grace": grace},
                name="unidl-download-cleanup",
                daemon=True,
            )
            self._shutdown_thread.start()

    def shutdown(self, *, grace: float = 0.15) -> None:
        """Stop this delivery and all of its external resources.

        The grace period is deliberately measured in milliseconds.  It gives a
        well-behaved child a chance to close its descriptors, then force-kills
        it (including its process group on POSIX).
        """
        self._stop.set()
        self._network.cancel()
        self.interrupt_live_capture()
        self.close(grace=grace)

    def close(self, *, grace: float = 0.15) -> None:
        """Release a completed delivery without marking it cancelled."""
        with self._lock:
            cleanup = self._shutdown_thread
        if cleanup is not None and cleanup is not threading.current_thread():
            cleanup.join()
        self._close_resources(grace=grace)

    def _close_resources(self, *, grace: float) -> None:
        with self._cleanup_lock:
            self._close_resources_locked(grace=grace)

    def _close_resources_locked(self, *, grace: float) -> None:
        with self._lock:
            self._closed = True
            closers = list(self._closers.values())
            self._closers.clear()
            self._resources.clear()
            processes = list(self._processes.values())
        # Do not let a slow transport closer delay signalling child processes.
        for process, process_group in processes:
            self._signal_process(process, process_group)
        deadline = time.monotonic() + max(0.0, float(grace))
        while time.monotonic() < deadline:
            if all(process.poll() is not None for process, _group in processes):
                break
            time.sleep(0.01)
        for process, process_group in processes:
            if process.poll() is None:
                self._signal_process(process, process_group, force=True)
        for closer in closers:
            try:
                closer()
            except Exception:
                pass
        for process, _group in processes:
            try:
                process.wait(timeout=0.15)
            except (OSError, subprocess.TimeoutExpired):
                pass
        with self._lock:
            self._processes.clear()

    @staticmethod
    def _signal_process(process: subprocess.Popen, process_group: bool, *, force: bool = False) -> None:
        """Stop a child without evaluating POSIX-only signals on Windows."""
        if process.poll() is not None:
            return
        if process_group and os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            if force:
                process.kill()
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass

    @classmethod
    def _terminate_process(cls, process: subprocess.Popen, process_group: bool) -> None:
        cls._signal_process(process, process_group)
        try:
            process.wait(timeout=0.05)
        except (OSError, subprocess.TimeoutExpired):
            cls._signal_process(process, process_group, force=True)


def current_download_runtime() -> DownloadRuntime | None:
    return DownloadRuntime.current()


def transfer_auth_status(error: BaseException) -> int | None:
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        status = error.code if isinstance(error, HTTPError) else getattr(error, "http_status", None)
        if status in (401, 403):
            return status
        if error.__cause__ is None:
            break
        error = error.__cause__
    return None


def abort_expired_vod_transfer(error: BaseException, *, phase: str = "segment") -> None:
    runtime = current_download_runtime()
    if runtime is None or runtime.vod_transfer is None:
        return
    status = transfer_auth_status(error)
    if status is not None:
        # Keep the typed root cause even when a cancelled sibling reaches the
        # owner first. Shut down this attempt, not the enclosing delivery.
        runtime.vod_transfer.note_failure(status, phase)
        runtime.request_shutdown()
        raise DownloadCancelled("VOD playback authorization expired") from error


def with_download_runtime(function):
    """A direct downloader call needs the same transfer-local resources as the CLI."""
    @wraps(function)
    def run(*args, **kwargs):
        if current_download_runtime() is not None:
            return function(*args, **kwargs)
        with DownloadRuntime().activate():
            return function(*args, **kwargs)

    return run


_proxy_environment: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "unidl_downloader_proxy_environment", default=None,
)
_proxy_policy: contextvars.ContextVar[tuple[str | None, bool]] = contextvars.ContextVar(
    "unidl_downloader_proxy_policy", default=(None, False),
)


def current_proxy_policy() -> tuple[str | None, bool]:
    return _proxy_policy.get()


@contextlib.contextmanager
def proxy_scope(custom_proxy: str | None, use_system_proxy: bool = True):
    """Preserve downloader proxy options without changing the host environment."""
    overrides = None
    if custom_proxy:
        overrides = dict.fromkeys(("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"), custom_proxy)
        overrides.update({"NO_PROXY": "", "no_proxy": ""})
    elif not use_system_proxy:
        overrides = {"NO_PROXY": "*", "no_proxy": "*"}
    token = _proxy_environment.set(overrides)
    policy_token = _proxy_policy.set((custom_proxy, use_system_proxy))
    try:
        yield
    finally:
        _proxy_policy.reset(policy_token)
        _proxy_environment.reset(token)


def _apply_child_proxy_environment(kwargs: dict) -> None:
    overrides = _proxy_environment.get()
    if overrides:
        env = kwargs.get("env")
        kwargs["env"] = {**(os.environ if env is None else env), **overrides}


class DownloaderThreadPoolExecutor(ThreadPoolExecutor):
    """Carry the submitting download's context to each worker invocation."""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


def managed_popen(*args, **kwargs) -> subprocess.Popen:
    """Start a downloader child registered to the active delivery runtime."""
    _apply_child_proxy_environment(kwargs)
    runtime = current_download_runtime()
    if runtime is None:
        return subprocess.Popen(*args, **kwargs)
    if os.name == "posix" and "start_new_session" not in kwargs:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(*args, **kwargs)
    return runtime.register_process(
        process,
        process_group=bool(kwargs.get("start_new_session", False)),
    )


def managed_run(*popenargs, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` with runtime registration when embedded."""
    runtime = current_download_runtime()
    if runtime is None:
        _apply_child_proxy_environment(kwargs)
        return subprocess.run(*popenargs, **kwargs)
    input_data = kwargs.pop("input", None)
    timeout = kwargs.pop("timeout", None)
    check = kwargs.pop("check", False)
    capture_output = kwargs.pop("capture_output", False)
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    process_group = os.name == "posix" and kwargs.get("start_new_session", True)
    process = managed_popen(*popenargs, **kwargs)
    try:
        stdout, stderr = process.communicate(input=input_data, timeout=timeout)
    except BaseException:
        # A timeout, cancellation or interrupted pipe read must not orphan the
        # child. Cleanup must also preserve the original exception instead of
        # replacing a useful probe/mux diagnostic with a secondary wait error.
        runtime._signal_process(process, bool(process_group), force=True)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=0.15)
        raise
    finally:
        # If termination failed or has not completed, keep runtime ownership so
        # shutdown can retry rather than silently forgetting a running child.
        if process.poll() is not None:
            runtime.unregister_process(process)
    result = subprocess.CompletedProcess(popenargs[0] if popenargs else kwargs.get("args"), process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args, output=stdout, stderr=stderr)
    return result


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """Normalized progress shared by VOD and live recording paths."""

    stream: StreamInfo
    completed_segments: int
    total_segments: int | None
    downloaded_bytes: int
    total_bytes: int | None
    elapsed_seconds: float
    speed_bytes_per_second: float | None = None
    eta_seconds: float | None = None
    live: bool = False
    recorded_seconds: float | None = None
    duration_seconds: float | None = None
    status: str = "Downloading"
    done: bool = False


@dataclass(frozen=True, slots=True)
class DownloadArtifact:
    """A final media or sidecar file that survived post-processing."""

    path: Path
    kind: str = "media"
    stream: StreamInfo | None = None


@dataclass(frozen=True, slots=True)
class DownloadMessage:
    """A console-independent message emitted by the native engine.

    ``transient`` marks a replaceable status row such as muxing or decrypting;
    hosts may paint it in place instead of appending it to a permanent log.
    """

    text: str
    level: str = "info"
    transient: bool = False


@dataclass(frozen=True, slots=True)
class DownloadHooks:
    """Optional host callbacks used by the embedded download API."""

    live_key_provider: LiveKeyProvider | None = None
    artifact_created: Callable[[DownloadArtifact], None] | None = None
    progress: Callable[[DownloadProgress], None] | None = None
    message: Callable[[DownloadMessage], None] | None = None
    cancel_requested: Callable[[], bool] | None = None
    pause_requested: Callable[[], bool] | None = None
    runtime: DownloadRuntime | None = None
    display_width: Callable[[], int | None] | None = None
    console_progress: bool = True
    console_output: bool = True
    stop_recording_requested: Callable[[], bool] | None = None
    renew_live_source: Callable[[LiveSourceExpired], LiveSourceRenewal | None] | None = None


__all__ = [
    "DownloadArtifact",
    "DownloadCancelled",
    "DownloadHooks",
    "DownloadMessage",
    "DownloadProgress",
    "DownloadRuntime",
    "LiveKeyProvider",
    "LiveKeyRequest",
    "LiveRecordingStopped",
    "current_download_runtime",
    "managed_popen",
    "managed_run",
]

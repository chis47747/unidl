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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SegmentInfo, StreamInfo


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


LiveKeyProvider = Callable[[LiveKeyRequest], str | None]


class DownloadCancelled(RuntimeError):
    """The embedding application requested an orderly download stop."""


class DownloadRuntime:
    """Own the resources which must die with one embedded download.

    Cooperative cancellation is still useful for preserving a clean partial
    download, but it cannot interrupt a socket read or a child process.  The
    runtime is the hard-stop side of that contract: it closes registered
    transports and terminates registered process groups immediately.
    """

    _active_lock = threading.RLock()
    _active: dict[int, DownloadRuntime] = {}
    _current: contextvars.ContextVar[DownloadRuntime | None] = contextvars.ContextVar(
        "unidl_download_runtime",
        default=None,
    )

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._shutdown_thread: threading.Thread | None = None
        self._processes: dict[int, tuple[subprocess.Popen, bool]] = {}
        self._closers: dict[int, Callable[[], None]] = {}
        self._next_closer = 0

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
    def activate(self):
        """Make this runtime visible to downloader worker threads.

        ``ThreadPoolExecutor`` workers do not inherit context variables.  The
        active-runtime fallback therefore remains process-local and is used
        only when exactly one embedded download is active, which is UniDL's
        normal delivery model; the context variable is preferred on the owner
        thread.
        """
        token = self._current.set(self)
        with self._active_lock:
            self._active[id(self)] = self
        try:
            yield self
        finally:
            self.close()
            self._current.reset(token)
            with self._active_lock:
                self._active.pop(id(self), None)

    @classmethod
    def current(cls) -> DownloadRuntime | None:
        runtime = cls._current.get()
        if runtime is not None:
            return runtime
        with cls._active_lock:
            if len(cls._active) == 1:
                return next(iter(cls._active.values()))
        return None

    def register_process(self, process: subprocess.Popen, *, process_group: bool = False) -> subprocess.Popen:
        with self._lock:
            if self.stopped:
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
            if not self.stopped:
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
            closers = list(self._closers.values())
            self._closers.clear()
            processes = list(self._processes.values())
        # Do not let a slow transport closer delay signalling child processes.
        for process, process_group in processes:
            self._signal_process(process, process_group)
        for closer in closers:
            try:
                closer()
            except Exception:
                pass
        deadline = time.monotonic() + max(0.0, float(grace))
        while time.monotonic() < deadline:
            if all(process.poll() is not None for process, _group in processes):
                break
            time.sleep(0.01)
        for process, process_group in processes:
            if process.poll() is None:
                self._signal_process(process, process_group, force=True)
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


def managed_popen(*args, **kwargs) -> subprocess.Popen:
    """Start a downloader child registered to the active delivery runtime."""
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

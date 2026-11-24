"""Small, shared primitives for sensitive local state.

Tokens, settings, credentials, cookies, content keys and exported commands are
different formats, but they have the same filesystem contract: owner-only,
never half-written, and never merged concurrently without a lock.  Keeping that
contract here avoids each store growing a subtly different ``.tmp`` protocol.
"""

from __future__ import annotations

import contextlib
import errno
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

try:  # POSIX cross-process locking.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows installations
    fcntl = None  # type: ignore[assignment]

try:  # Windows cross-process locking.
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX installations
    msvcrt = None  # type: ignore[assignment]

FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_held_paths = threading.local()


def set_fd_mode(descriptor: int, mode: int) -> bool:
    """Apply a file-descriptor mode where the platform supports it.

    Windows has no :func:`os.fchmod`. Named paths are chmod'ed after atomic
    replacement, so skipping this fd-only step on Windows keeps the write path
    portable without weakening the final path-level handling.
    """
    chmod = getattr(os, "fchmod", None)
    if chmod is None:
        return False
    try:
        chmod(descriptor, mode)
    except (OSError, NotImplementedError):
        return False
    return True


def safe_filename(value: object, *, label: str = "file name") -> str:
    """Return one portable file name, never a path or a special component."""
    raw = str(value or "").strip()
    invalid = set('<>:"/\\|?*')
    if (
        not raw
        or raw in {".", ".."}
        or Path(raw).is_absolute()
        or Path(raw).name != raw
        or any(character in invalid or ord(character) < 32 for character in raw)
    ):
        raise ValueError(f"{label} must be one safe file name, not {value!r}")
    return raw


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.abspath(path)
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def private_directory(path: Path) -> Path:
    """Create ``path`` and make the directory owner-only where supported."""
    path = Path(path)
    path.mkdir(parents=True, mode=DIRECTORY_MODE, exist_ok=True)
    try:
        path.chmod(DIRECTORY_MODE)
    except OSError:
        pass
    return path


def private_file(path: Path) -> Path:
    """Make an existing regular file owner-only without following a symlink."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return path
    try:
        path.chmod(FILE_MODE)
    except OSError:
        pass
    return path


def secure_tree(root: Path) -> None:
    """Tighten a runtime-state tree without following symbolic links."""
    root = private_directory(root)
    for current_name, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_name)
        for name in list(directory_names):
            path = current / name
            if path.is_symlink():
                directory_names.remove(name)
                continue
            try:
                path.chmod(DIRECTORY_MODE)
            except OSError:
                pass
        for name in file_names:
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.chmod(FILE_MODE)
            except OSError:
                pass


def _acquire_file_lock(descriptor: int) -> bool:
    """Acquire an OS-level exclusive lock on ``descriptor`` when supported."""
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return True
    if msvcrt is not None:  # pragma: no cover - exercised on Windows installations
        delay = 0.01
        while True:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EDEADLK, errno.EAGAIN}:
                    raise
                time.sleep(delay)
                delay = min(0.05, delay * 1.5)
    return False


def _release_file_lock(descriptor: int) -> None:
    """Release an OS-level exclusive lock previously acquired on ``descriptor``."""
    if fcntl is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
    elif msvcrt is not None:  # pragma: no cover - exercised on Windows installations
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextlib.contextmanager
def locked_path(path: Path) -> Iterator[None]:
    """Hold an in-process and cross-process exclusive lock for ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    key = os.path.abspath(lock_path)
    thread_lock = _thread_lock(lock_path)
    with thread_lock:
        held: set[str] = getattr(_held_paths, "keys", None)  # type: ignore[assignment]
        if held is None:
            held = set()
            _held_paths.keys = held
        if key in held:
            # Same thread already owns the OS lock on this path; re-opening and
            # re-locking a second descriptor would deadlock on POSIX and Windows.
            yield
            return
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(lock_path, flags, FILE_MODE)
        locked = False
        try:
            set_fd_mode(descriptor, FILE_MODE)
            locked = _acquire_file_lock(descriptor)
            held.add(key)
            yield
        finally:
            held.discard(key)
            if locked:
                _release_file_lock(descriptor)
            os.close(descriptor)


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = FILE_MODE) -> Path:
    """Replace ``path`` with one complete owner-only byte sequence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        set_fd_mode(descriptor, mode)
        handle = os.fdopen(descriptor, "wb")
        descriptor = -1
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
        _sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return path


def atomic_write_text(
    path: Path, text: str, *, encoding: str = "utf-8", mode: int = FILE_MODE
) -> Path:
    return atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_via(
    path: Path, writer: Callable[[Path], None], *, mode: int = FILE_MODE
) -> Path:
    """Atomically replace ``path`` using a library that insists on a file name."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        set_fd_mode(descriptor, mode)
        os.close(descriptor)
        descriptor = -1
        writer(temporary)
        try:
            temporary.chmod(mode)
        except OSError:
            pass
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
        _sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return path


__all__ = [
    "DIRECTORY_MODE",
    "FILE_MODE",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_via",
    "locked_path",
    "private_directory",
    "private_file",
    "safe_filename",
    "set_fd_mode",
    "secure_tree",
]

"""Token / state cache for services.

One directory, ``paths.tokens``, is authoritative after a service has adopted
any explicitly named legacy file. :meth:`TokenStore.import_legacy` performs
that adoption once, only while the destination does not exist; all later reads,
writes and sign-outs use the destination alone.

That boundary is worth being strict about rather than lenient. A store that
keeps reading an older location has to answer "which copy is the session" every
time, and the two answers that follow - the read finds one file, the sign-out
clears the other - add up to a sign-out that does not work.

Token directories are owner-only and writes are atomic. A token file carries the
same authority as a password while its refresh token is alive, so another local
account must not be able to read it; and an interrupted refresh must leave either
the complete old JSON or the complete new JSON, never a truncated session.

Keys (KID/key) live in the project's key vault (``paths.keys_db``) and exported
download commands under ``paths.commands``, both inside the project.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def secure_token_tree(root: Path) -> None:
    """Create and secure a token directory and every real entry below it.

    Symbolic links are never followed. Token state belongs inside this tree; a
    link would both escape the permission boundary and make sign-out ambiguous.
    Files that disappear during the walk are harmless, but any other chmod error
    is surfaced rather than continuing with credentials known to be world-readable.
    """
    root = Path(root)
    if root.is_symlink():
        raise OSError(f"token directory must not be a symbolic link: {root}")
    root.mkdir(parents=True, mode=_DIRECTORY_MODE, exist_ok=True)
    root.chmod(_DIRECTORY_MODE)

    for current_name, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_name)
        try:
            current.chmod(_DIRECTORY_MODE)
        except FileNotFoundError:
            continue

        for name in list(directory_names):
            path = current / name
            if path.is_symlink():
                directory_names.remove(name)
                continue
            try:
                path.chmod(_DIRECTORY_MODE)
            except FileNotFoundError:
                directory_names.remove(name)

        for name in file_names:
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.chmod(_FILE_MODE)
            except FileNotFoundError:
                continue


class TokenStore:
    def __init__(self, cache_dir: Path, legacy_dirs: list[Path] | tuple[Path, ...] = ()):
        self.cache_dir = Path(cache_dir)
        secure_token_tree(self.cache_dir)
        self.legacy_dirs = tuple(Path(path) for path in legacy_dirs if Path(path) != self.cache_dir)

    def path(self, name: str) -> Path:
        raw = str(name or "").strip()
        if (
            not raw
            or raw in {".", ".."}
            or "/" in raw
            or "\\" in raw
            or "\0" in raw
            or Path(raw).is_absolute()
            or Path(raw).name != raw
        ):
            raise ValueError(f"token name must be one file, not a path: {name!r}")
        return self.cache_dir / raw

    def resolve(self, name: str) -> Path | None:
        """The regular file behind ``name``, if there is one."""
        own = self.path(name)
        if not own.is_symlink() and own.is_file():
            return own
        # A service id rename keeps the old directory readable exactly once.  A
        # successful JSON import is moved into the canonical store, so future
        # reads/writes/logout never consult two competing copies.
        for legacy_dir in self.legacy_dirs:
            source = legacy_dir / own.name
            if source.is_symlink() or not source.is_file():
                continue
            migrated = self.import_legacy(name, source)
            if migrated is not None:
                try:
                    source.unlink()
                except OSError:
                    pass
                return own
        return None

    def read(self, name: str) -> dict[str, Any] | None:
        path = self.resolve(name)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        data["_cache_file"] = str(path)
        return data

    def _atomic_write_text(self, path: Path, text: str) -> Path:
        """Write ``text`` as an owner-only file without exposing a partial value."""
        secure_token_tree(self.cache_dir)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(self.cache_dir)
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, _FILE_MODE)
            handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = -1
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(_FILE_MODE)
            self._sync_directory()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return path

    def _sync_directory(self) -> None:
        """Persist the rename where the filesystem supports directory fsync."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(self.cache_dir, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def write(self, name: str, data: dict[str, Any]) -> Path:
        payload = {k: v for k, v in data.items() if not str(k).startswith("_")}
        payload.setdefault("saved_at", utc_stamp())
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        return self._atomic_write_text(self.path(name), text)

    def import_legacy(self, name: str, source: Path) -> dict[str, Any] | None:
        """Copy one explicitly named legacy JSON file into this store once.

        The destination's existence is the migration marker. Even an unreadable
        destination or a signed-out tombstone blocks another import, so stale
        credentials in ``source`` can never make a logout come back to life.
        Symbolic-link sources are rejected rather than followed outside the
        caller's explicitly named legacy location.
        """
        destination = self.path(name)
        if destination.exists() or destination.is_symlink():
            return self.read(name)

        source = Path(source).expanduser()
        if source.is_symlink() or not source.is_file():
            return None
        try:
            data = json.loads(source.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None

        self.write(name, data)
        return self.read(name)

    def delete(self, name: str) -> bool:
        return self.remove(name)

    def remove(self, name: str) -> bool:
        """Delete a cached token. Returns whether there was one to delete."""
        own = self.path(name)
        if not own.exists() and not own.is_symlink():
            return False
        try:
            own.unlink()
        except OSError:
            return False
        return True

    def read_text(self, name: str) -> str | None:
        path = self.resolve(name)
        if path is None:
            return None
        try:
            return path.read_text("utf-8")
        except OSError:
            return None

    def write_text(self, name: str, text: str) -> Path:
        return self._atomic_write_text(self.path(name), text)

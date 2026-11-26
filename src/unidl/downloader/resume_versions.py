"""Strong HTTP media validators for the native VOD resume cache."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path


def strong_digest(etag: str | None) -> str:
    value = etag.strip() if isinstance(etag, str) else ""
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return ""
    return hashlib.sha256(value.encode()).hexdigest()


class VersionIndex:
    """Store only hashed resource IDs and their first observed strong ETag.

    The first version owns the original part fingerprints. Other versions get
    distinct fingerprints, so changing back to the original is safe too.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    @contextmanager
    def _open(self):
        # Don't hold a database handle while the task is cleaned up (Windows).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS media (resource TEXT PRIMARY KEY, tag TEXT NOT NULL)")
            yield db

    def snapshot(self) -> dict[str, str]:
        with self.lock:
            if not self.path.is_file():
                return {}
            with self._open() as db:
                return dict(db.execute("SELECT resource, tag FROM media"))

    def baseline(self, resource: str, digest: str) -> str:
        with self.lock, self._open() as db:
            db.execute("INSERT OR IGNORE INTO media VALUES (?, ?)", (resource, digest))
            return db.execute("SELECT tag FROM media WHERE resource = ?", (resource,)).fetchone()[0]


_observer: ContextVar[tuple[str, Callable[[str | None], None]] | None] = ContextVar(
    "native_media_etag_observer", default=None
)


@contextmanager
def observing_media(url: str, callback: Callable[[str | None], None]):
    token = _observer.set((url, callback))
    try:
        yield
    finally:
        _observer.reset(token)


def observe_media_response(url: str, etag: str | None) -> None:
    if (scope := _observer.get()) is not None and scope[0] == url:
        scope[1](etag)

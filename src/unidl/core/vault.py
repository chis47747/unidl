"""Local content key vault.

Borrowed from unshackle's vault design (`unshackle/core/vault.py`,
`unshackle/vaults/SQLite.py`, `docs/guide/vaults.md`):

* keys are matched by **KID, never by PSSH** - a PSSH box can differ between
  requests for the very same content, while a KID only changes when the media
  itself changes
* KID and key are stored as 32 lowercase hex characters with no dashes,
  compared ``COLLATE NOCASE``
* an all-zero key is treated as "no key": lookups skip it, writes reject it
* ``UNIQUE(kid, key)`` so re-adding is a no-op rather than a duplicate
* SQLite in WAL mode with ``synchronous=NORMAL`` and a 30s busy timeout, one
  connection per thread, so the UI thread and the download worker can share it

Deliberate deviation: unshackle gives every service **its own table** named
after the service tag. We use **one table with a ``service`` column** instead,
because:

* global "which title did this KID come from" search is a single indexed query
  rather than enumerating ``sqlite_master`` and querying every table (a
  limitation unshackle documents for its own ``kv search``)
* provenance is per row, not per table: title, source, cdm, timestamp
* no table name is ever interpolated into SQL (unshackle's own source carries
  three ``TODO: SQL injection risk`` comments about exactly that)
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .secureio import private_file

HEX32 = re.compile(r"^[0-9a-f]{32}$")
_NULL_KEY = "0" * 32
# Service ids are part of the vault namespace.  Renamed native services keep
# their old rows readable, while all newly licensed keys are written under the
# canonical id supplied by the service.
SERVICE_ID_ALIASES: dict[str, tuple[str, ...]] = {
    "appletv": ("apple",),
    "paramountplus": ("paramount",),
}
_LEGACY_SERVICE_IDS = {
    legacy: canonical
    for canonical, legacy_ids in SERVICE_ID_ALIASES.items()
    for legacy in legacy_ids
}
#: `--key kid:key` as emitted by UniDL and N_m3u8DL-RE
_KEY_PAIR = re.compile(r"--key[= ]+['\"]?([0-9a-fA-F]{32})[:\-]([0-9a-fA-F]{32})['\"]?")
#: `--save-name X` so an imported row can carry a title
_SAVE_NAME = re.compile(r"--save-name[= ]+(?:\"([^\"]+)\"|'([^']+)'|(\S+))")

# A pair sitting inside a command, JSON string or copied log line.  Both halves
# may be compact hex or UUID-shaped; :func:`normalize_hex` makes their stored
# representation identical.  ``split_pair`` remains the permissive one-pair
# fallback for labelled forms such as ``kid=<id> key=<key>``.
_HEX_ID = r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
_PAIRS_IN_TEXT = re.compile(
    rf"(?<![0-9a-fA-F])({_HEX_ID})\s*(?::|：|=|--|-)\s*({_HEX_ID})(?![0-9a-fA-F])"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kid        TEXT NOT NULL COLLATE NOCASE,
    key        TEXT NOT NULL COLLATE NOCASE,
    service    TEXT NOT NULL COLLATE NOCASE,
    title      TEXT,
    pssh       TEXT,
    source     TEXT NOT NULL DEFAULT 'license',
    cdm        TEXT,
    origin     TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kid, key)
);
CREATE INDEX IF NOT EXISTS keys_kid_idx ON keys(kid);
CREATE INDEX IF NOT EXISTS keys_service_idx ON keys(service);
CREATE INDEX IF NOT EXISTS keys_title_idx ON keys(title);
-- every read here is `ORDER BY created_at DESC`, and without this SQLite sorts
-- the whole matching set in a temp B-tree before the LIMIT can help
CREATE INDEX IF NOT EXISTS keys_created_idx ON keys(created_at);
"""


def normalize_hex(value: object) -> str | None:
    """Return a 32-char lowercase hex string, or None if it is not one."""
    text = str(value or "").strip().strip("'\"").replace("-", "").lower()
    return text if HEX32.match(text) else None


def playready_kid_alias(value: object) -> str | None:
    """Return the other common byte ordering of a PlayReady KID.

    PlayReady license APIs commonly expose a UUID in canonical order while an
    MP4 ``default_KID`` exposes the same GUID with its first three fields little
    endian. The transform is its own inverse, so it works in either direction.
    This is a search alias only; vault rows keep the KID actually returned by the
    licence path.
    """
    kid = normalize_hex(value)
    if not kid:
        return None
    alias = uuid.UUID(hex=kid).bytes_le.hex()
    return alias if alias != kid else None


#: a 32-hex id sitting inside other text, with nothing hexadecimal touching it
_ID_IN_TEXT = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])")


def service_names(service: str | None) -> tuple[str, ...]:
    """Canonical service id followed by ids retained for compatibility."""
    raw = str(service or "").strip()
    if not raw:
        return ()
    return tuple(dict.fromkeys((raw, *SERVICE_ID_ALIASES.get(raw.lower(), ()))))


def canonical_service(service: str | None) -> str:
    raw = str(service or "").strip()
    return _LEGACY_SERVICE_IDS.get(raw.lower(), raw)


def hex_ids(value: object) -> list[str]:
    """Every 32-hex id named anywhere in ``value``, lowercased, in order.

    :func:`normalize_hex` answers "is this string an id". This answers "does this
    string *name* one", which is the question a search box actually gets: people
    paste ``KID:39a1…``, ``kid=39a1… key=d428…``, or a line lifted out of a log with
    a label still attached. Reading the id out of that is not being clever about
    input, it is the difference between finding the key and being told there is no
    such key - which is what happened.
    """
    text = str(value or "").replace("-", "")
    found: list[str] = []
    for match in _ID_IN_TEXT.finditer(text):
        candidate = match.group(1).lower()
        if candidate not in found:
            found.append(candidate)
    return found


def is_null_key(key: str) -> bool:
    return not key or key == _NULL_KEY or key.count("0") == len(key)


def split_pair(value: str) -> tuple[str, str] | None:
    """Parse ``kid:key`` into normalized halves.

    The full-width colon is in the separator list because it is what a Chinese or
    Japanese keyboard produces without being asked, and a pair typed with one is
    still a pair. Failing that, two ids named anywhere in the text are read in the
    order they appear - ``kid=39a1… key=d428…`` is the same request as ``39a1…:d428…``
    and there is nothing to gain by only understanding one of them.
    """
    text = str(value or "").strip().strip("'\"")
    for sep in (":", "\uff1a", "--", "="):
        if sep in text:
            left, _, right = text.partition(sep)
            kid, key = normalize_hex(left), normalize_hex(right)
            if kid and key:
                return kid, key
    found = hex_ids(text)
    if len(found) == 2:
        return found[0], found[1]
    return None


@dataclass(frozen=True)
class KeyRecord:
    kid: str
    key: str
    service: str
    title: str | None = None
    pssh: str | None = None
    source: str = "license"
    cdm: str | None = None
    origin: str | None = None
    created_at: str = ""

    def pair(self) -> str:
        return f"{self.kid}:{self.key}"


@dataclass(frozen=True)
class PairIssue:
    """One input line that cannot safely become a stored key."""

    line: int
    reason: str


class PairParseError(ValueError):
    """A pasted batch contains one or more invalid lines."""

    def __init__(self, issues: Iterable[PairIssue]):
        self.issues = tuple(issues)
        lines = ", ".join(str(issue.line) for issue in self.issues)
        super().__init__(f"invalid key input on line(s) {lines}")


@dataclass(frozen=True)
class KeyConflict:
    """A KID already maps to a different key in the local vault."""

    kid: str
    incoming_key: str
    existing_keys: tuple[str, ...]
    services: tuple[str, ...]


class KeyConflictError(ValueError):
    """A batch needs explicit replacement before it can be stored."""

    def __init__(self, conflicts: Iterable[KeyConflict]):
        self.conflicts = tuple(conflicts)
        kids = ", ".join(conflict.kid for conflict in self.conflicts)
        super().__init__(f"conflicting key already stored for KID(s): {kids}")


@dataclass(frozen=True)
class KeyBatchPreview:
    """What a normalized manual batch would do without changing the vault."""

    pairs: tuple[str, ...]
    new: tuple[str, ...]
    existing: tuple[str, ...]
    conflicts: tuple[KeyConflict, ...]


@dataclass(frozen=True)
class KeyWriteResult:
    """Counts from one atomic local-vault write."""

    total: int
    added: int
    existing: int
    replaced: int


def parse_pairs(text: str) -> list[str]:
    """Parse one or many pasted key pairs, or raise with exact line numbers.

    One pair per line is the normal form, but copied command lines and JSON lists
    may carry several direct ``KID:key`` pairs on one line.  Every non-empty line
    has to yield at least one pair: silently skipping a typo in a bulk write would
    make a partial import look complete.
    """

    parsed: list[tuple[str, str, int]] = []
    issues: list[PairIssue] = []
    for number, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        matches = [
            (normalize_hex(match.group(1)), normalize_hex(match.group(2)))
            for match in _PAIRS_IN_TEXT.finditer(line)
        ]
        pairs = [(kid, key) for kid, key in matches if kid and key]
        if not pairs:
            single = split_pair(line)
            pairs = [single] if single else []
        if not pairs:
            issues.append(PairIssue(number, "expected KID:key"))
            continue
        for kid, key in pairs:
            if is_null_key(key):
                issues.append(PairIssue(number, "the content key is all zero"))
                continue
            parsed.append((kid, key, number))

    by_kid: dict[str, tuple[str, int]] = {}
    for kid, key, number in parsed:
        previous = by_kid.get(kid)
        if previous is not None and previous[0] != key:
            issues.append(PairIssue(number, "the same KID has another key in this batch"))
        else:
            by_kid[kid] = (key, number)
    if issues:
        raise PairParseError(issues)
    return [f"{kid}:{key}" for kid, (key, _line) in by_kid.items()]


class _Connections:
    """One connection per thread, matching unshackle's ConnectionFactory."""

    def __init__(self, path: Path):
        self._path = path
        self._local = threading.local()

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            conn.commit()
            for path in (
                self._path,
                self._path.with_name(self._path.name + "-wal"),
                self._path.with_name(self._path.name + "-shm"),
            ):
                private_file(path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's connection, if it opened one."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        conn.close()
        del self._local.conn


class KeyVault:
    def __init__(self, path: Path):
        self.path = Path(path)
        private_file(self.path)
        self._connections = _Connections(self.path)

    def close(self) -> None:
        """Close the calling thread's SQLite connection."""
        self._connections.close()

    # ------------------------------------------------------------------ write
    def add(
        self,
        service: str,
        kid: str,
        key: str,
        *,
        title: str | None = None,
        pssh: str | None = None,
        source: str = "license",
        cdm: str | None = None,
        origin: str | None = None,
    ) -> bool:
        """Store one KID:key. Returns True when a new row was inserted."""
        kid_hex, key_hex = normalize_hex(kid), normalize_hex(key)
        if not kid_hex or not key_hex or is_null_key(key_hex):
            return False
        conn = self._connections.get()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO keys
                (kid, key, service, title, pssh, source, cdm, origin, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kid_hex,
                key_hex,
                str(service or "unknown"),
                title,
                pssh,
                source,
                cdm,
                origin,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cursor.rowcount > 0

    def add_pairs(
        self,
        service: str,
        pairs: Iterable[str],
        **meta,
    ) -> int:
        """Store ``kid:key`` strings, the shape services and UniDL already use."""
        added = 0
        for entry in pairs:
            parsed = split_pair(entry)
            if parsed and self.add(service, parsed[0], parsed[1], **meta):
                added += 1
        return added

    @staticmethod
    def _normalized_entries(pairs: Iterable[str]) -> list[tuple[str, str]]:
        """Validated, de-duplicated pairs with one unambiguous key per KID."""
        entries: dict[str, str] = {}
        for value in pairs:
            pair = split_pair(str(value))
            if pair is None or is_null_key(pair[1]):
                raise ValueError("every vault entry must be a non-zero KID:key pair")
            previous = entries.get(pair[0])
            if previous is not None and previous != pair[1]:
                raise ValueError(f"the batch gives KID {pair[0]} more than one key")
            entries[pair[0]] = pair[1]
        return list(entries.items())

    @staticmethod
    def _preview(
        conn: sqlite3.Connection, entries: Iterable[tuple[str, str]]
    ) -> KeyBatchPreview:
        pairs: list[str] = []
        new: list[str] = []
        existing: list[str] = []
        conflicts: list[KeyConflict] = []
        for kid, key in entries:
            pair = f"{kid}:{key}"
            pairs.append(pair)
            rows = conn.execute(
                "SELECT key, service FROM keys WHERE kid = ? ORDER BY created_at DESC, id DESC",
                (kid,),
            ).fetchall()
            same = any(str(row["key"]).lower() == key for row in rows)
            different = [row for row in rows if str(row["key"]).lower() != key]
            (existing if same else new).append(pair)
            if different:
                conflicts.append(
                    KeyConflict(
                        kid=kid,
                        incoming_key=key,
                        existing_keys=tuple(dict.fromkeys(str(row["key"]).lower() for row in different)),
                        services=tuple(dict.fromkeys(str(row["service"]) for row in different)),
                    )
                )
        return KeyBatchPreview(tuple(pairs), tuple(new), tuple(existing), tuple(conflicts))

    def preview_pairs(self, pairs: Iterable[str]) -> KeyBatchPreview:
        """Inspect a manual batch without writing any of it."""
        entries = self._normalized_entries(pairs)
        return self._preview(self._connections.get(), entries)

    def add_many(
        self,
        service: str,
        pairs: Iterable[str],
        *,
        title: str | None = None,
        pssh: str | None = None,
        source: str = "manual",
        cdm: str | None = None,
        origin: str | None = None,
        replace_conflicts: bool = False,
    ) -> KeyWriteResult:
        """Atomically store a validated batch, optionally replacing conflicts.

        Validation happens before ``BEGIN IMMEDIATE`` and the conflict check is
        repeated inside that transaction.  Consequently another writer cannot
        slip a conflicting row between the preview shown by the TUI and this
        commit, and a failed batch never leaves a valid-looking partial import.
        """
        entries = self._normalized_entries(pairs)
        if not entries:
            return KeyWriteResult(0, 0, 0, 0)
        conn = self._connections.get()
        try:
            conn.execute("BEGIN IMMEDIATE")
            preview = self._preview(conn, entries)
            if preview.conflicts and not replace_conflicts:
                raise KeyConflictError(preview.conflicts)
            replaced = 0
            if replace_conflicts:
                for conflict in preview.conflicts:
                    cursor = conn.execute(
                        "DELETE FROM keys WHERE kid = ? AND key != ?",
                        (conflict.kid, conflict.incoming_key),
                    )
                    replaced += max(0, cursor.rowcount)
            created = datetime.now(timezone.utc).isoformat(timespec="seconds")
            added = 0
            for kid, key in entries:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO keys
                        (kid, key, service, title, pssh, source, cdm, origin, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kid,
                        key,
                        str(service or "unknown"),
                        title,
                        pssh,
                        source,
                        cdm,
                        origin,
                        created,
                    ),
                )
                added += max(0, cursor.rowcount)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return KeyWriteResult(
            total=len(entries),
            added=added,
            existing=len(entries) - added,
            replaced=replaced,
        )

    # ------------------------------------------------------------------- read
    def get_key(self, kid: str, service: str | None = None) -> str | None:
        """The key for a KID, preferring a same-service match."""
        kid_hex = normalize_hex(kid)
        if not kid_hex:
            return None
        conn = self._connections.get()
        for service_name in service_names(service):
            row = conn.execute(
                """
                SELECT key FROM keys
                WHERE kid = ? AND service = ? AND key != ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (kid_hex, service_name, _NULL_KEY),
            ).fetchone()
            if row:
                return row["key"]
        row = conn.execute(
            """
            SELECT key FROM keys
            WHERE kid = ? AND key != ?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (kid_hex, _NULL_KEY),
        ).fetchone()
        return row["key"] if row else None

    def get_keys(self, kids: Iterable[str], service: str | None = None) -> dict[str, str]:
        """Batch lookup. Returns only the KIDs that were found."""
        found: dict[str, str] = {}
        for kid in kids:
            kid_hex = normalize_hex(kid)
            if not kid_hex or kid_hex in found:
                continue
            key = self.get_key(kid_hex, service)
            if key:
                found[kid_hex] = key
        return found

    def find(self, needle: str, *, service: str | None = None, limit: int = 50) -> list[KeyRecord]:
        """Look up by KID, by ``kid:key`` pair, or by title text."""
        conn = self._connections.get()
        clauses: list[str] = []
        params: list[object] = []

        pair = split_pair(needle)
        kid_hex = normalize_hex(needle)
        named = hex_ids(needle)
        if pair:
            kids = tuple(dict.fromkeys((pair[0], playready_kid_alias(pair[0]))))
            marks = ", ".join("?" for _ in kids)
            clauses.append(f"(kid IN ({marks}) AND key = ?)")  # noqa: S608 - counted marks
            params += [*kids, pair[1]]
        elif kid_hex:
            kids = tuple(dict.fromkeys((kid_hex, playready_kid_alias(kid_hex))))
            marks = ", ".join("?" for _ in kids)
            clauses.append(f"(kid IN ({marks}) OR key = ?)")  # noqa: S608 - counted marks
            params += [*kids, kid_hex]
        elif named:
            # An id with a label still on it - "KID: 39a1…" - is a lookup, not a
            # title. Searching the titles for it finds nothing, which reads as "that
            # key is not here" about a key that is.
            kids = tuple(
                dict.fromkeys(
                    candidate
                    for value in named
                    for candidate in (value, playready_kid_alias(value))
                    if candidate
                )
            )
            kid_marks = ", ".join("?" for _ in kids)
            key_marks = ", ".join("?" for _ in named)
            clauses.append(  # noqa: S608 - counted marks
                f"(kid IN ({kid_marks}) OR key IN ({key_marks}))"
            )
            params += [*kids, *named]
        else:
            text = str(needle or "").strip()
            if not text:
                return []
            clauses.append("(title LIKE ? OR service LIKE ?)")
            params += [f"%{text}%", f"%{text}%"]

        where = " AND ".join(clauses)
        names = service_names(service)
        if names:
            marks = ", ".join("?" for _ in names)
            where += f" AND service IN ({marks})"  # noqa: S608 - counted marks
            params.extend(names)

        rows = conn.execute(
            f"SELECT * FROM keys WHERE {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608 - fixed clauses
            (*params, limit),
        ).fetchall()
        return [self._record(row) for row in rows]

    def count_for(self, service: str) -> int:
        """How many keys one service has.

        Counted in SQLite rather than by measuring a list of records: the screen
        that shows this only wants the number, and building 17,000 dataclasses to
        call ``len`` on them took 95ms and 15MB on the UI thread.
        """
        conn = self._connections.get()
        names = service_names(service)
        if not names:
            return 0
        marks = ", ".join("?" for _ in names)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM keys WHERE service IN ({marks})",  # noqa: S608 - counted marks
            names,
        ).fetchone()
        return int(row["n"]) if row else 0

    def by_service(self, service: str, limit: int = 500) -> list[KeyRecord]:
        conn = self._connections.get()
        names = service_names(service)
        if not names:
            return []
        marks = ", ".join("?" for _ in names)
        rows = conn.execute(
            f"SELECT * FROM keys WHERE service IN ({marks}) ORDER BY created_at DESC LIMIT ?",  # noqa: S608 - counted marks
            (*names, limit),
        ).fetchall()
        return [self._record(row) for row in rows]

    def stats(self) -> dict:
        conn = self._connections.get()
        total = conn.execute("SELECT COUNT(*) AS n FROM keys").fetchone()["n"]
        rows = conn.execute(
            "SELECT service, COUNT(*) AS n FROM keys GROUP BY service ORDER BY n DESC"
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            name = canonical_service(row["service"])
            counts[name] = counts.get(name, 0) + int(row["n"])
        return {
            "keys": total,
            "services": len(counts),
            "by_service": sorted(counts.items(), key=lambda item: (-item[1], item[0])),
        }

    @staticmethod
    def _record(row: sqlite3.Row) -> KeyRecord:
        return KeyRecord(
            kid=row["kid"],
            key=row["key"],
            service=row["service"],
            title=row["title"],
            pssh=row["pssh"],
            source=row["source"],
            cdm=row["cdm"],
            origin=row["origin"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _title_from(text: str, path: Path) -> str:
        match = _SAVE_NAME.search(text)
        if match:
            return next(group for group in match.groups() if group)
        # "Some.Title_20260727_114307.txt" -> "Some.Title"
        return re.sub(r"_\d{8}_\d{6}$", "", path.stem)

    def iter_all(self) -> Iterator[KeyRecord]:
        conn = self._connections.get()
        for row in conn.execute("SELECT * FROM keys ORDER BY created_at DESC"):
            yield self._record(row)


__all__ = [
    "KeyBatchPreview",
    "KeyConflict",
    "KeyConflictError",
    "KeyRecord",
    "KeyVault",
    "KeyWriteResult",
    "PairIssue",
    "PairParseError",
    "is_null_key",
    "normalize_hex",
    "playready_kid_alias",
    "parse_pairs",
    "split_pair",
]

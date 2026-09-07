"""More than one place to look for a content key.

:mod:`unidl.core.vault` is the local SQLite store and stays exactly that. This is
the layer above it: an ordered list of vaults, asked in turn, following
unshackle's design (``unshackle/core/vaults.py``, ``unshackle/vaults/API.py``):

* a **read** walks selected local entries first, then selected remote entries,
  preserving config order inside each group and taking the first non-null key
* a **write** goes to every selected vault that accepts one, which is how a
  shared vault gets populated by whoever happened to download the title
* ``no_push: true`` marks a vault as read-only from here - useful for a vault
  somebody else maintains, and for one you do not want your own keys leaving to

Two backends, because two is what the shapes are: a **local** one (SQLite, the
default, and the only one configured out of the box) and an **api** one (HTTP,
the shape unshackle's ``API`` vault speaks, so an existing vault server works
without a server-side change).

A remote vault is a network call in the middle of a key lookup, so failure has to
be boring: any error from a remote vault is logged and skipped, never raised. A
vault being down means "no cached key", not "the download fails".
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests

from .secureio import private_directory
from .vault import KeyRecord, KeyVault, canonical_service, is_null_key, normalize_hex, split_pair

LineSink = Callable[[str], None]


class Vault(Protocol):
    """What the collection needs of a vault. Every backend satisfies it."""

    name: str
    no_push: bool
    #: True when reaching this vault leaves the machine. It is what the remote
    #: switch turns off, and it is a property of the backend rather than of the
    #: configuration - "is this a network call" is not something a user should
    #: have to declare per entry.
    remote: bool
    searchable: bool
    enabled: bool

    def get_keys(self, kids: Iterable[str], service: str | None = None) -> dict[str, str]: ...

    def add_pairs(self, service: str, pairs: Iterable[str], **meta: Any) -> int: ...

    def describe(self) -> str: ...


# ------------------------------------------------------------------ local


class LocalVault:
    """The SQLite vault, wrapped so it looks like every other one.

    A thin adapter rather than changes to :class:`KeyVault`: that class is also
    the provenance store behind the key screen and the ``keys`` command, and it
    has a wider interface than a vault needs. Keeping the two apart means the
    remote backends do not have to grow a ``find`` or an importer they cannot
    implement.
    """

    remote = False
    searchable = True

    def __init__(
        self,
        vault: KeyVault,
        name: str = "local",
        no_push: bool = False,
        enabled: bool = True,
    ):
        self.vault = vault
        self.name = name
        self.no_push = no_push
        self.enabled = enabled

    @property
    def path(self) -> Path:
        return self.vault.path

    def get_keys(self, kids: Iterable[str], service: str | None = None) -> dict[str, str]:
        return self.vault.get_keys(kids, service)

    def add_pairs(self, service: str, pairs: Iterable[str], **meta: Any) -> int:
        return self.vault.add_pairs(service, pairs, **meta)

    def describe(self) -> str:
        return f"{self.name}  sqlite  {self.vault.path}"


# -------------------------------------------------------------------- api


class ApiVault:
    """A vault behind an HTTP API, in the shape unshackle's ``API`` vault uses.

    ``GET  {uri}/{service}/{kid}``  -> ``{"code": 0, "content_key": "<hex>"}``
    ``POST {uri}/{service}/{kid}``  -> ``{"code": 0, "added": true}``

    A non-zero ``code`` is the server saying no, and the message it sends with it
    is more useful than anything invented here, so it is passed through.
    """

    #: what the server's code field means, from unshackle's table
    CODES = {
        0: "",
        1: "the token was rejected",
        2: "rate limited",
        3: "the service tag is not one it knows",
        4: "the key id is invalid",
        5: "the content key is invalid",
    }

    remote = True
    searchable = False

    def __init__(
        self,
        uri: str,
        token: str = "",
        name: str = "api",
        no_push: bool = False,
        timeout: float = 15.0,
        enabled: bool = True,
    ):
        self.uri = str(uri or "").rstrip("/")
        self.name = name
        self.no_push = no_push
        self.enabled = enabled
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "unidl"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def describe(self) -> str:
        suffix = "  read-only" if self.no_push else ""
        return f"{self.name}  api  {self.uri}{suffix}"

    def get_keys(self, kids: Iterable[str], service: str | None = None) -> dict[str, str]:
        found: dict[str, str] = {}
        tag = (service or "").lower()
        for kid in kids:
            kid_hex = normalize_hex(kid)
            if not kid_hex or kid_hex in found:
                continue
            data = self._call("get", f"{self.uri}/{tag}/{kid_hex}")
            key = normalize_hex(data.get("content_key"))
            if key and not is_null_key(key):
                found[kid_hex] = key
        return found

    def add_pairs(self, service: str, pairs: Iterable[str], **meta: Any) -> int:
        tag = (service or "").lower()
        added = 0
        for entry in pairs:
            parsed = split_pair(entry)
            if not parsed:
                continue
            kid, key = parsed
            data = self._call(
                "post", f"{self.uri}/{tag}/{kid}", json={"content_key": key}
            )
            if data.get("added") or data.get("updated"):
                added += 1
        return added

    def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            raise VaultError(f"{self.name}: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise VaultError(f"{self.name}: the answer was not JSON") from exc
        if not isinstance(data, dict):
            raise VaultError(f"{self.name}: the answer was not an object")
        code = int(data.get("code") or 0)
        if code:
            reason = self.CODES.get(code) or f"code {code}"
            message = str(data.get("message") or "").strip()
            raise VaultError(f"{self.name}: {reason}{f' - {message}' if message else ''}")
        return data


# ------------------------------------------------------------------- http


class HttpVault:
    """A vault behind unshackle's ``HTTP`` backend, in either of its two modes.

    A different protocol from :class:`ApiVault` despite both being HTTP, which is
    why it is a separate backend rather than a flag: everything goes to **one URL**
    and the operation is named in the body.

    ``json`` mode - one POST per call::

        {"method": "GetKey", "params": {"kid": ..., "service": ..., "session_id": ...},
         "token": "<password>"}
        -> {"status_code": 200, "message": {"keys": [{"kid": ..., "key": ...}]}}

        {"method": "InsertKey", "params": {"kid": ..., "key": ..., "service": ...,
         "title": ...}, "token": "<password>"}
        -> {"status_code": 200, "message": {"inserted": true}}

    ``query`` mode puts the same fields in a GET query string and needs a
    ``username`` as well as a ``password``.

    Two details taken from a live server rather than from the reference:

    * ``status_code`` is inside the body and an HTTP 200 can still carry a
      failure, so the body is what decides
    * a rejected token is an HTTP **401** with a plain message, and an unknown
      method is a **400** with ``error``/``type`` instead of ``message`` - so both
      field names are read when reporting why
    """

    remote = True

    def __init__(
        self,
        host: str,
        password: str = "",
        username: str = "",
        api_mode: str = "json",
        name: str = "http",
        no_push: bool = False,
        timeout: float = 15.0,
        supported_services: Iterable[str] | None = None,
        service_map: Mapping[str, str] | None = None,
        searchable: bool = False,
        enabled: bool = True,
    ):
        self.url = str(host or "").strip()
        if not self.url:
            raise ValueError("an HTTP vault needs a host")
        self.password = str(password or "")
        if not self.password:
            raise ValueError("an HTTP vault needs a password")
        self.username = str(username or "")
        self.api_mode = (api_mode or "json").strip().lower()
        if self.api_mode not in ("json", "query"):
            raise ValueError(f"unknown api_mode {api_mode!r}; use json or query")
        if self.api_mode == "query" and not self.username:
            raise ValueError("query mode needs a username as well as a password")
        self.name = name
        self.no_push = no_push
        self.enabled = enabled
        self.timeout = timeout
        self.supported_services = tuple(
            dict.fromkeys(
                str(value).strip().lower()
                for value in (supported_services or ())
                if str(value).strip()
            )
        )
        self.service_map = {
            str(source).strip().lower(): str(target).strip().lower()
            for source, target in (service_map or {}).items()
            if str(source).strip() and str(target).strip()
        }
        # Global search is deliberately opt-in per backend. ``get_keys`` being
        # available is not enough: the home screen has no service tag, so a
        # backend must also publish a finite supported-service list that can be
        # searched explicitly without inventing requests it never promised.
        self.searchable = bool(searchable and self.supported_services)
        #: the server may hand out a session id to carry between calls. This one
        #: answers ``null``; keeping it costs nothing and one that uses it works.
        self.session_id: Any = None
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "unidl"})

    def describe(self) -> str:
        suffix = "  read-only" if self.no_push else ""
        return f"{self.name}  http/{self.api_mode}  {self.url}{suffix}"

    # ------------------------------------------------------------------ read
    def get_keys(self, kids: Iterable[str], service: str | None = None) -> dict[str, str]:
        tag = self._service_tag(service)
        if tag is None:
            return {}
        found: dict[str, str] = {}
        for kid in kids:
            kid_hex = normalize_hex(kid)
            if not kid_hex or kid_hex in found:
                continue
            answer = self._call("GetKey", {"kid": kid_hex, "service": tag, "title": None})
            key = _key_for(kid_hex, answer.get("keys"))
            if key and not is_null_key(key):
                found[kid_hex] = key
        return found

    @property
    def search_services(self) -> tuple[str, ...]:
        """Service tags that may narrow an explicit home-screen key search."""
        return self.supported_services if self.searchable else ()

    def search_keys(
        self,
        kids: Iterable[str],
        *,
        service: str | None = None,
    ) -> list[VaultSearchResult]:
        """Search exact KIDs across all or one declared service namespace.

        This is intentionally separate from :meth:`get_keys`: playback always
        knows its service and makes one lookup, while home search knows only the
        KID and may fan out. The caller runs it only after an explicit click.
        Passing a declared ``service`` makes exactly one request per candidate
        KID, which is the safe route for a service-aware HTTP vault.
        """
        if not self.searchable:
            return []
        candidates = tuple(
            dict.fromkeys(
                kid
                for kid in (normalize_hex(value) for value in kids)
                if kid
            )
        )
        requested = self._service_tag(service) if service else None
        if service and requested is None:
            return []
        services = (requested,) if requested else self.supported_services
        for kid in candidates:
            for service_tag in services:
                answer = self._call(
                    "GetKey",
                    {"kid": kid, "service": service_tag, "title": None},
                )
                key = _key_for(kid, answer.get("keys"))
                if key and not is_null_key(key):
                    return [VaultSearchResult(self.name, kid, key, service_tag)]
        return []

    # ----------------------------------------------------------------- write
    def add_pairs(self, service: str, pairs: Iterable[str], **meta: Any) -> int:
        tag = self._service_tag(service)
        if tag is None:
            return 0
        title = meta.get("title")
        added = 0
        for entry in pairs:
            parsed = split_pair(entry)
            if not parsed:
                continue
            kid, key = parsed
            answer = self._call(
                "InsertKey", {"kid": kid, "key": key, "service": tag, "title": title}
            )
            # "cached" without "inserted" is a key it already had, which is a
            # success from here: the point was for the vault to hold it.
            if answer.get("inserted") or answer.get("status") == "cached":
                added += 1
        return added

    def _service_tag(self, service: str | None) -> str | None:
        raw = str(service or "").strip().lower()
        tag = self.service_map.get(raw, raw)
        if self.supported_services and tag not in self.supported_services:
            return None
        return tag

    # -------------------------------------------------------------- plumbing
    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.api_mode == "query":
            response = self.session.get(
                self.url,
                params={
                    **{k: v for k, v in params.items() if v is not None},
                    "username": self.username,
                    "password": self.password,
                },
                timeout=self.timeout,
            )
        else:
            response = self.session.post(
                self.url,
                json={
                    "method": method,
                    "params": {**params, "session_id": self.session_id},
                    "token": self.password,
                },
                timeout=self.timeout,
            )
        if response.status_code == 404:
            return {}  # nothing there is not an error
        try:
            data = response.json()
        except ValueError as exc:
            raise VaultError(
                f"{self.name}: {method} answered {response.status_code} with "
                f"something that is not JSON"
            ) from exc
        if not isinstance(data, dict):
            raise VaultError(f"{self.name}: {method} answered with a {type(data).__name__}")

        status = str(data.get("status_code") or response.status_code)
        if status != "200":
            reason = str(
                data.get("error") or data.get("message") or response.reason or ""
            ).strip()
            raise VaultError(f"{self.name}: {method} refused ({status} {reason})")

        message = data.get("message")
        if not isinstance(message, dict):
            return data if isinstance(data, dict) else {}
        if message.get("session_id"):
            self.session_id = message["session_id"]
        return message


def _key_for(kid: str, keys: Any) -> str | None:
    """Pull one key out of whatever shape the vault listed them in.

    Three are in use: ``[{"kid": ..., "key": ...}]``, ``["kid:key"]``, and a bare
    map. Accepting all three costs a few lines and means a vault that answers in a
    neighbouring dialect still works.
    """
    if isinstance(keys, dict):
        return normalize_hex(keys.get(kid) or keys.get("key"))
    for entry in keys or []:
        if isinstance(entry, dict):
            if normalize_hex(entry.get("kid")) == kid:
                return normalize_hex(entry.get("key"))
        elif isinstance(entry, str) and ":" in entry:
            parsed = split_pair(entry)
            if parsed and parsed[0] == kid:
                return parsed[1]
    return None


class VaultError(RuntimeError):
    """A vault said no, or could not be reached."""


@dataclass(frozen=True)
class VaultWriteResult:
    """One backend's answer to a multi-vault write."""

    name: str
    remote: bool
    added: int = 0
    existing: int = 0
    replaced: int = 0
    skipped: str = ""
    error: str = ""


@dataclass(frozen=True)
class VaultSearchResult:
    """One exact key found through an explicitly searched remote backend."""

    vault: str
    kid: str
    key: str
    service: str


@dataclass(frozen=True)
class VaultDescriptor:
    """A configured destination, safe to show in settings without opening it."""

    name: str
    remote: bool
    writable: bool = True
    detail: str = ""
    searchable: bool = True
    enabled: bool = True
    #: Local SQLite path, when this descriptor represents one. Remote backends
    #: leave it empty; the TUI uses it to import discovered databases without
    #: opening or exposing their contents.
    path: Path | None = None
    #: True for a safe, read-only discovery candidate that is not in YAML yet.
    discovered: bool = False


def _vault_definitions(config) -> list[tuple[dict[str, Any] | None, VaultDescriptor]]:
    """Normalize config entries once for both settings and runtime construction."""
    specs = list(config.vault_specs())
    if not specs:
        return [
            (
                None,
                VaultDescriptor(
                    "local",
                    remote=False,
                    detail="SQLite · paths.keys_db",
                    path=config.paths.keys_db,
                ),
            )
        ]

    found: list[tuple[dict[str, Any] | None, VaultDescriptor]] = []
    used: set[str] = set()
    for spec in specs:
        kind = str(spec.get("type") or "").strip().lower()
        name = str(spec.get("name") or kind or "vault").strip() or "vault"
        if name.casefold() in used:
            raise ValueError(
                f"key_vaults names must be unique; {name!r} appears more than once"
            )
        used.add(name.casefold())
        remote = kind in ("http", "httpapi", "api")
        writable = not bool(spec.get("no_push"))
        enabled = bool(spec.get("enabled", True))
        searchable = not remote or bool(
            kind in ("http", "httpapi")
            and (spec.get("searchable") or spec.get("search_keys"))
            and spec.get("supported_services")
        )
        if remote:
            detail = f"{kind.upper()} · remote"
            local_path = None
        else:
            configured_path = spec.get("path")
            database = Path(str(configured_path)).name if configured_path else "default database"
            detail = f"SQLite · {database}"
            local_path = _local_path(config, configured_path)
        found.append(
            (
                spec,
                VaultDescriptor(
                    name,
                    remote=remote,
                    writable=writable,
                    searchable=searchable,
                    enabled=enabled,
                    detail=detail,
                    path=local_path,
                ),
            )
        )
    return found


def configured_vaults(config) -> list[VaultDescriptor]:
    """Return configured vault names for settings and destination pickers."""
    return [descriptor for _spec, descriptor in _vault_definitions(config)]


_SQLITE_HEADER = b"SQLite format 3\x00"


def _looks_like_key_vault(path: Path) -> bool:
    """Return whether ``path`` is a readable UniDL SQLite key database.

    Discovery must not open arbitrary files in a project ``db`` folder as a
    writable vault. The header check avoids most false positives; the read-only
    schema probe then confirms the table the :class:`KeyVault` adapter owns.
    """
    try:
        with path.open("rb") as handle:
            if handle.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                return False
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.5) as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'keys' LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            columns = {
                str(item[1]).casefold()
                for item in connection.execute("PRAGMA table_info(keys)")
            }
        return {"kid", "key", "service", "created_at"}.issubset(columns)
    except (OSError, sqlite3.Error):
        return False


def _discovery_detail(path: Path, roots: Iterable[Path]) -> str:
    """Return a short, useful database label for the TUI import list."""
    for root in roots:
        try:
            relative = path.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return f"SQLite · db/{relative}"
    return f"SQLite · {path.name}"


def discovered_local_vaults(config) -> list[VaultDescriptor]:
    """Find unconfigured local key databases for the TUI import picker.

    Only direct project state roots are considered: ``<paths.home>/db`` and,
    when different, the source checkout's ``db`` folder. Existing configured
    paths are omitted, so the same database never appears twice. Candidates are
    descriptors only; callers must explicitly save them to ``key_vaults`` before
    the runtime opens them.
    """
    configured = configured_vaults(config)
    # Exclude configured paths even when a database was temporarily moved or
    # has not been created yet.  Discovery is a presentation aid, not a second
    # way to resurrect a configured entry under a different generated name.
    configured_paths: set[Path] = set()
    for descriptor in configured:
        if descriptor.remote or descriptor.path is None:
            continue
        try:
            configured_paths.add(descriptor.path.expanduser().resolve())
        except OSError:
            configured_paths.add(Path(os.path.abspath(descriptor.path.expanduser())))
    roots: list[Path] = [Path(config.paths.home) / "db"]
    source = getattr(config, "source", None)
    if source is not None:
        roots.append(Path(source).parent / "db")

    candidates: list[Path] = []
    seen_paths: set[Path] = set()
    for root in roots:
        try:
            paths = sorted(root.rglob("*"), key=lambda item: str(item).casefold())
        except OSError:
            continue
        for path in paths:
            # Do not rely on a filename suffix.  A copied vault may be named
            # ``archive`` or ``keys-vault.sqlite3``; the SQLite header and
            # schema probe below are the type check, and the scan is confined
            # to the project's db/ state directory.
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen_paths or resolved in configured_paths:
                continue
            seen_paths.add(resolved)
            if _looks_like_key_vault(resolved):
                candidates.append(resolved)

    used_names = {descriptor.name.casefold() for descriptor in configured}
    found: list[VaultDescriptor] = []
    for path in candidates:
        name = path.stem
        if name.casefold() in used_names:
            relative = path.parent.name or "db"
            name = f"{name} ({relative})"
        suffix = 2
        base = name
        while name.casefold() in used_names:
            name = f"{base} {suffix}"
            suffix += 1
        used_names.add(name.casefold())
        found.append(
            VaultDescriptor(
                name=name,
                remote=False,
                writable=True,
                detail=_discovery_detail(path, roots),
                searchable=True,
                enabled=True,
                path=path,
                discovered=True,
            )
        )
    return found


def _local_path(config, value: object) -> Path:
    """Resolve one configured SQLite path beside its YAML when relative."""
    if not value:
        return config.paths.keys_db
    path = Path(os.path.expandvars(str(value))).expanduser()
    if not path.is_absolute() and getattr(config, "source", None) is not None:
        path = Path(config.source).parent / path
    return path


def parse_targets(value: object) -> tuple[str, ...] | None:
    """Parse a stored multi-vault value.

    ``None`` means all vaults in the relevant local/remote category.  The
    explicit ``__none__`` marker means no destination, which is useful when a
    user wants to force a licence request without storing its result.
    """
    text = str(value or "").strip()
    if not text or text.casefold() == "all":
        return None
    names: list[str] = []
    for item in text.split(","):
        name = item.strip()
        if not name:
            continue
        if name.casefold() == "__none__":
            return ()
        if name.casefold() not in {existing.casefold() for existing in names}:
            names.append(name)
    return tuple(names)


def serialize_targets(value: tuple[str, ...] | None) -> str:
    """Serialize a target selection for ``settings.json``."""
    if value is None:
        return ""
    return ",".join(value) if value else "__none__"


# ------------------------------------------------------------- collection


@dataclass
class Vaults:
    """Every configured vault, in the order they were configured."""

    vaults: list[Vault] = field(default_factory=list)
    log: LineSink = lambda _line: None

    def __len__(self) -> int:
        return len(self.vaults)

    def __iter__(self):
        return iter(self.vaults)

    @property
    def writable(self) -> list[Vault]:
        return [vault for vault in self.vaults if not vault.no_push]

    @property
    def local(self) -> list[Vault]:
        return [vault for vault in self.vaults if not vault.remote]

    @property
    def remote(self) -> list[Vault]:
        return [vault for vault in self.vaults if vault.remote]

    @staticmethod
    def _selected(vaults: list[Vault], names: Iterable[str] | None) -> list[Vault]:
        available = [vault for vault in vaults if getattr(vault, "enabled", True)]
        if names is None:
            return available
        wanted = {str(name).strip().casefold() for name in names if str(name).strip()}
        return [vault for vault in available if vault.name.casefold() in wanted]

    def enabled(
        self,
        *,
        use_local: bool = True,
        use_remote: bool = True,
        local_names: Iterable[str] | None = None,
        remote_names: Iterable[str] | None = None,
    ) -> list[Vault]:
        """The vaults in play, **local first**.

        Order is fixed here rather than taken from the config file, and that is the
        answer to "what happens when both are on": a local hit is free and offline,
        so it is always tried first, and the network is only reached for the key
        ids the local store did not have. A config that lists a remote vault first
        does not get to make every lookup a round trip.
        """
        chosen: list[Vault] = []
        if use_local:
            chosen += self._selected(self.local, local_names)
        if use_remote:
            chosen += self._selected(self.remote, remote_names)
        return chosen

    def get_keys(
        self,
        kids: Iterable[str],
        service: str | None = None,
        *,
        use_local: bool = True,
        use_remote: bool = True,
        backfill: bool = True,
        local_names: Iterable[str] | None = None,
        remote_names: Iterable[str] | None = None,
        backfill_local_names: Iterable[str] | None = None,
    ) -> dict[str, str]:
        """First answer wins, per KID, local before remote.

        Asked vault by vault rather than KID by KID so a local hit costs nothing
        and the network is only reached for what is actually still missing.

        ``backfill`` writes a key that came from a remote vault into the local one.
        That is what makes the second download of a title offline, and it is on by
        default because a key is not worth fetching twice - but it only ever copies
        *inward*, so nothing leaves the machine as a side effect of a lookup.
        """
        wanted = [kid for kid in (normalize_hex(k) for k in kids) if kid]
        vaults = self.enabled(
            use_local=use_local,
            use_remote=use_remote,
            local_names=local_names,
            remote_names=remote_names,
        )
        found: dict[str, str] = {}
        from_remote: dict[str, str] = {}
        for vault in vaults:
            missing = [kid for kid in wanted if kid not in found]
            if not missing:
                break
            try:
                answers = vault.get_keys(missing, service)
            except Exception as exc:  # noqa: BLE001 - a dead vault is not a failure
                self.log(f"vault {vault.name}: unavailable ({exc})")
                continue
            fresh = {
                kid: key
                for kid, key in answers.items()
                if key and not is_null_key(key) and kid not in found
            }
            found.update(fresh)
            if fresh and vault.remote:
                from_remote.update(fresh)
            if fresh and len(vaults) > 1:
                self.log(f"vault {vault.name}: {len(fresh)} of {len(missing)} keys")
        if backfill and from_remote:
            self._backfill(service, from_remote, local_names=backfill_local_names)
        return found

    def search_remote(
        self,
        kids: Iterable[str],
        *,
        name: str,
        service: str | None = None,
    ) -> list[VaultSearchResult]:
        """Explicitly search one named, capability-declared remote vault."""
        selected = self._selected(self.remote, (name,))
        if not selected:
            configured = next(
                (vault for vault in self.remote if vault.name.casefold() == str(name).casefold()),
                None,
            )
            if configured is not None and not getattr(configured, "enabled", True):
                raise VaultError(f"remote vault {name!r} is disabled")
            raise VaultError(f"remote vault {name!r} is not configured")
        backend = selected[0]
        if not getattr(backend, "searchable", False):
            raise VaultError(f"{backend.name} does not declare key search support")
        search = getattr(backend, "search_keys", None)
        if not callable(search):
            raise VaultError(f"{backend.name} has no remote key search implementation")
        if service:
            scopes = tuple(getattr(backend, "search_services", ()) or ())
            if not scopes:
                raise VaultError(f"{backend.name} cannot narrow key search by service")
            return list(search(kids, service=service) or [])
        return list(search(kids) or [])

    def remote_search_services(self, name: str) -> tuple[str, ...]:
        """Declared service tags that can narrow one remote search request.

        This is a capability query only: it performs no I/O and deliberately
        does not infer support from an endpoint merely having a ``get_keys``
        method.
        """
        selected = self._selected(self.remote, (name,))
        if not selected:
            return ()
        backend = selected[0]
        if not getattr(backend, "searchable", False):
            return ()
        return tuple(
            dict.fromkeys(
                str(service).strip().lower()
                for service in (getattr(backend, "search_services", ()) or ())
                if str(service).strip()
            )
        )

    # ---------------------------------------------------------- local index
    @staticmethod
    def _record_identity(record: KeyRecord) -> tuple[str, str, str, str, str]:
        """Stable identity used when the same row exists in several SQLite vaults."""
        return (
            record.kid,
            record.key,
            canonical_service(record.service),
            record.title or "",
            record.source,
        )

    def _local_records(
        self,
        method: str,
        *args: Any,
        local_names: Iterable[str] | None = None,
        limit: int = 50,
        **kwargs: Any,
    ) -> list[KeyRecord]:
        """Merge a provenance query across selected local SQLite vaults."""
        found: dict[tuple[str, str, str, str, str], KeyRecord] = {}
        for backend in self._selected(self.local, local_names):
            store = getattr(backend, "vault", None)
            query = getattr(store, method, None)
            if not callable(query):
                continue
            try:
                records = query(*args, limit=limit, **kwargs)
            except Exception as exc:  # noqa: BLE001 - one damaged optional DB should not hide the rest
                self.log(f"vault {backend.name}: could not search ({exc})")
                continue
            for record in records:
                found.setdefault(self._record_identity(record), record)
        return sorted(found.values(), key=lambda record: record.created_at, reverse=True)[:limit]

    def find_local(
        self,
        needle: str,
        *,
        service: str | None = None,
        limit: int = 50,
        local_names: Iterable[str] | None = None,
    ) -> list[KeyRecord]:
        """Search every selected local vault and de-duplicate mirrored rows."""
        return self._local_records(
            "find",
            needle,
            service=service,
            limit=limit,
            local_names=local_names,
        )

    def by_service_local(
        self,
        service: str,
        *,
        limit: int = 500,
        local_names: Iterable[str] | None = None,
    ) -> list[KeyRecord]:
        return self._local_records(
            "by_service",
            service,
            limit=limit,
            local_names=local_names,
        )

    def stats_local(self, *, local_names: Iterable[str] | None = None) -> dict[str, Any]:
        """Aggregate counts for the search screen without double-counting mirrors."""
        found: dict[tuple[str, str, str], KeyRecord] = {}
        for backend in self._selected(self.local, local_names):
            store = getattr(backend, "vault", None)
            iterate = getattr(store, "iter_all", None)
            if not callable(iterate):
                continue
            try:
                for record in iterate():
                    identity = (record.kid, record.key, canonical_service(record.service))
                    found.setdefault(identity, record)
            except Exception as exc:  # noqa: BLE001
                self.log(f"vault {backend.name}: could not count ({exc})")
        counts: dict[str, int] = {}
        for record in found.values():
            service = canonical_service(record.service)
            counts[service] = counts.get(service, 0) + 1
        return {
            "keys": len(found),
            "services": len(counts),
            "by_service": sorted(counts.items(), key=lambda item: (-item[1], item[0])),
        }

    def count_for_local(
        self,
        service: str,
        *,
        local_names: Iterable[str] | None = None,
    ) -> int:
        return len(
            self.by_service_local(
                service,
                limit=2_147_483_647,
                local_names=local_names,
            )
        )

    def _backfill(
        self,
        service: str | None,
        keys: dict[str, str],
        *,
        local_names: Iterable[str] | None = None,
    ) -> None:
        """Copy keys a remote vault supplied into the local one."""
        pairs = [f"{kid}:{key}" for kid, key in keys.items()]
        for vault in self._selected(self.local, local_names):
            if vault.no_push:
                continue
            try:
                stored = vault.add_pairs(service or "unknown", pairs, source="vault")
            except Exception as exc:  # noqa: BLE001
                self.log(f"vault {vault.name}: could not cache the remote keys ({exc})")
                continue
            if stored:
                self.log(f"vault {vault.name}: cached {stored} key(s) from the remote vault")

    def add_pairs(
        self,
        service: str,
        pairs: Iterable[str],
        *,
        use_local: bool = True,
        use_remote: bool = True,
        local_names: Iterable[str] | None = None,
        remote_names: Iterable[str] | None = None,
        **meta: Any,
    ) -> int:
        """Push to every enabled vault that takes writes. Returns the local count.

        The local number is the one reported because it is the only one that means
        the same thing every time - a remote vault may answer "added" for a key it
        already had, or not answer at all.

        ``use_remote=False`` stops the write as well as the read, which is the one
        place the two switches behave differently: not reading a local file is a
        preference, but sending keys to somebody else's server is an action, and a
        switch that is off should not be taking it.
        """
        reports = self.add_pairs_report(
            service,
            pairs,
            use_local=use_local,
            use_remote=use_remote,
            local_names=local_names,
            remote_names=remote_names,
            **meta,
        )
        local = [report.added for report in reports if not report.remote and not report.error]
        remote = [report.added for report in reports if report.remote and not report.error]
        return max(local or remote or [0])

    def add_pairs_report(
        self,
        service: str,
        pairs: Iterable[str],
        *,
        use_local: bool = True,
        use_remote: bool = True,
        local_names: Iterable[str] | None = None,
        remote_names: Iterable[str] | None = None,
        **meta: Any,
    ) -> list[VaultWriteResult]:
        """Push a batch and retain each destination's result for the TUI.

        Ordinary playback still uses :meth:`add_pairs` and its compact integer
        answer. A manual write needs to say which remote accepted, skipped or
        rejected it; flattening those outcomes into one count would make a local
        success look like a complete remote success.
        """
        entries = list(pairs)
        reports: list[VaultWriteResult] = []
        for vault in self.enabled(
            use_local=use_local,
            use_remote=use_remote,
            local_names=local_names,
            remote_names=remote_names,
        ):
            if vault.no_push:
                reports.append(VaultWriteResult(vault.name, vault.remote, skipped="read only"))
                continue
            try:
                count = vault.add_pairs(service, entries, **meta)
            except Exception as exc:  # noqa: BLE001
                self.log(f"vault {vault.name}: could not store ({exc})")
                reports.append(VaultWriteResult(vault.name, vault.remote, error=str(exc)))
                continue
            reports.append(VaultWriteResult(vault.name, vault.remote, added=count))
        return reports

    def describe(self) -> list[str]:
        return [vault.describe() for vault in self.vaults]

    def close(self) -> None:
        """Close every local SQLite handle, without closing a shared object twice."""
        closed: set[int] = set()
        for vault in self.local:
            if id(vault) in closed:
                continue
            closed.add(id(vault))
            close = getattr(vault, "vault", None)
            if close is not None and callable(getattr(close, "close", None)):
                close.close()


def build(config, local: KeyVault | None = None, log: LineSink | None = None) -> Vaults:
    """The configured vaults, local first unless the config says otherwise.

    With no ``key_vaults`` section there is exactly one vault - the local SQLite
    file - which is the behaviour that existed before this module and the one
    every check depends on. A configured list replaces that default entirely, so
    a config can put a shared vault first, or leave the local one out.
    """
    sink: LineSink = log or (lambda _line: None)
    definitions = _vault_definitions(config)
    if definitions and definitions[0][0] is None:
        return Vaults([LocalVault(local or KeyVault(config.paths.keys_db), name="local")], log=sink)

    built: list[Vault] = []
    local_consumed = False
    for spec, descriptor in definitions:
        if spec is None:
            continue
        kind = str(spec.get("type") or "").strip().lower()
        name = descriptor.name
        no_push = bool(spec.get("no_push"))
        enabled = bool(spec.get("enabled", True))
        try:
            if kind in ("sqlite", "local", ""):
                path = spec.get("path")
                resolved = _local_path(config, path)
                private_directory(resolved.parent)
                vault = (
                    local
                    if local is not None and not path and not local_consumed
                    else KeyVault(resolved)
                )
                if local is not None and not path and not local_consumed:
                    local_consumed = True
                built.append(LocalVault(vault, name=name, no_push=no_push, enabled=enabled))
            elif kind in ("http", "httpapi"):
                built.append(
                    HttpVault(
                        host=str(spec.get("host") or spec.get("uri") or ""),
                        password=str(
                            spec.get("password") or spec.get("api_key") or spec.get("token") or ""
                        ),
                        username=str(spec.get("username") or ""),
                        api_mode=str(spec.get("api_mode") or "json"),
                        name=name,
                        no_push=no_push,
                        timeout=float(spec.get("timeout") or 15.0),
                        supported_services=spec.get("supported_services"),
                        service_map=spec.get("service_map"),
                        searchable=descriptor.searchable,
                        enabled=enabled,
                    )
                )
            elif kind == "api":
                built.append(
                    ApiVault(
                        uri=str(spec.get("uri") or spec.get("host") or ""),
                        token=str(spec.get("token") or spec.get("secret") or ""),
                        name=name,
                        no_push=no_push,
                        timeout=float(spec.get("timeout") or 15.0),
                        enabled=enabled,
                    )
                )
            else:
                sink(f"vault {name}: unknown type {kind!r}, ignored")
        except Exception as exc:  # noqa: BLE001 - a bad entry must not stop startup
            sink(f"vault {name}: could not be set up ({exc})")
    if not built:
        built = [LocalVault(local or KeyVault(config.paths.keys_db))]
    return Vaults(built, log=sink)


__all__ = [
    "ApiVault",
    "HttpVault",
    "LocalVault",
    "Vault",
    "VaultError",
    "VaultWriteResult",
    "VaultSearchResult",
    "VaultDescriptor",
    "Vaults",
    "build",
    "configured_vaults",
    "discovered_local_vaults",
    "parse_targets",
    "serialize_targets",
]

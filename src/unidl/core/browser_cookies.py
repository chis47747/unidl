"""Read-only browser Cookie & localStorage extraction (Firefox profiles).

Copies SQLite databases (plus ``-wal``/``-shm`` sidecars) into a private temp
folder before querying so live browser sessions are never locked or modified.
"""

from __future__ import annotations

import configparser
import os
import platform
import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any

from .cookies import CookieError, CookieStore


@dataclass(frozen=True, slots=True)
class FirefoxProfile:
    """One discovered Firefox browser profile directory."""

    name: str
    path: Path
    is_default: bool = False


@dataclass(slots=True)
class BrowserExtractResult:
    """Summary of cookies (and optional localStorage) extracted from a browser."""

    profile: Path
    cookies_count: int
    domains: tuple[str, ...] = ()
    saved_path: Path | None = None
    local_storage: dict[str, dict[str, str]] = field(default_factory=dict)


def default_firefox_roots() -> list[Path]:
    """Return standard Firefox configuration roots for the current OS."""
    system = platform.system().lower()
    home = Path.home()
    if system == "darwin":
        return [home / "Library" / "Application Support" / "Firefox"]
    if system == "windows":
        appdata = os.environ.get("APPDATA", "")
        roots = [Path(appdata) / "Mozilla" / "Firefox"] if appdata else []
        roots.append(home / "AppData" / "Roaming" / "Mozilla" / "Firefox")
        return list(dict.fromkeys(roots))
    return [
        home / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]


def discover_firefox_profiles(roots: Sequence[Path] | None = None) -> list[FirefoxProfile]:
    """Discover Firefox profiles from ``profiles.ini`` and fallback directory scan."""
    found: list[FirefoxProfile] = []
    seen: set[Path] = set()
    for root in (roots if roots is not None else default_firefox_roots()):
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        ini = root / "profiles.ini"
        if ini.is_file():
            parser = configparser.RawConfigParser()
            try:
                parser.read(ini, encoding="utf-8")
            except (OSError, configparser.Error):
                parser = configparser.RawConfigParser()
            for section in parser.sections():
                if not section.lower().startswith("profile"):
                    continue
                rel_path = parser.get(section, "Path", fallback="").strip()
                if not rel_path:
                    continue
                is_rel = parser.get(section, "IsRelative", fallback="1").strip() == "1"
                profile_path = (root / rel_path) if is_rel else Path(rel_path)
                if not profile_path.is_dir():
                    continue
                resolved = profile_path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                name = parser.get(section, "Name", fallback=profile_path.name).strip() or profile_path.name
                is_default = parser.get(section, "Default", fallback="0").strip() == "1"
                found.append(FirefoxProfile(name=name, path=resolved, is_default=is_default))
        # Fallback: direct directories containing cookies.sqlite
        for base in (root, root / "Profiles"):
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
                if not child.is_dir() or not (child / "cookies.sqlite").is_file():
                    continue
                resolved = child.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                found.append(
                    FirefoxProfile(
                        name=child.name,
                        path=resolved,
                        is_default="default" in child.name.lower(),
                    )
                )
    found.sort(key=lambda item: (not item.is_default, item.name.casefold()))
    return found


def _snapshot_sqlite(db_path: Path, work_dir: Path) -> Path:
    """Copy a SQLite DB and any WAL/SHM files into ``work_dir`` for lock-free reads."""
    dest = work_dir / db_path.name
    shutil.copy2(db_path, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_file():
            try:
                shutil.copy2(sidecar, dest.with_name(dest.name + suffix))
            except OSError:
                pass
    return dest


def _domain_matches(host: str, targets: Sequence[str]) -> bool:
    if not targets:
        return True
    clean_host = host.strip().lower().lstrip(".")
    for raw in targets:
        target = str(raw or "").strip().lower().lstrip(".")
        if not target:
            continue
        if clean_host == target or clean_host.endswith("." + target):
            return True
    return False


def extract_firefox_cookies(
    profile_dir: Path,
    domains: Sequence[str] = (),
) -> MozillaCookieJar:
    """Read matching cookies from a Firefox profile's ``cookies.sqlite`` into a jar."""
    profile_dir = Path(profile_dir).expanduser()
    db_path = profile_dir / "cookies.sqlite"
    if not db_path.is_file():
        raise CookieError(f"No Firefox cookies.sqlite found in {profile_dir}")

    jar = MozillaCookieJar()
    with tempfile.TemporaryDirectory(prefix="unidl-ff-cookies-") as tmp:
        snap = _snapshot_sqlite(db_path, Path(tmp))
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT host, path, isSecure, expiry, name, value FROM moz_cookies"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CookieError(f"Could not read Firefox cookies: {exc}") from exc
        finally:
            conn.close()

    for host, path, is_secure, expiry, name, value in rows:
        host_s = str(host or "").strip()
        name_s = str(name or "").strip()
        if not host_s or not name_s:
            continue
        if not _domain_matches(host_s, domains):
            continue
        cookie = Cookie(
            version=0,
            name=name_s,
            value=str(value or ""),
            port=None,
            port_specified=False,
            domain=host_s,
            domain_specified=host_s.startswith("."),
            domain_initial_dot=host_s.startswith("."),
            path=str(path or "/"),
            path_specified=True,
            secure=bool(is_secure),
            expires=int(expiry) if expiry and int(expiry) > 0 else None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
    return jar


def _decode_webappsstore_origin(origin_key: str) -> str:
    """Decode Firefox's legacy reversed-host ``originKey`` (e.g. ``moc.elpmaxe.:https:443``)."""
    parts = str(origin_key or "").split(":")
    if len(parts) < 2:
        return str(origin_key or "")
    rev_host = parts[0]
    scheme = parts[1] or "https"
    port = parts[2] if len(parts) >= 3 else ""
    host = rev_host[::-1].lstrip(".")
    default_port = (scheme == "https" and port == "443") or (scheme == "http" and port == "80")
    suffix = f":{port}" if port and not default_port else ""
    return f"{scheme}://{host}{suffix}" if host else str(origin_key or "")


def _decode_lsng_dir_origin(dir_name: str) -> str:
    """Decode Firefox LSNG storage dir name (e.g. ``https+++example.com`` -> ``https://example.com``)."""
    if "+++" not in dir_name:
        return dir_name
    scheme, rest = dir_name.split("+++", 1)
    host_part = rest.split("^", 1)[0]
    return f"{scheme}://{host_part.replace('+', ':')}"


def _decode_lsng_value(raw: Any, *, compression_type: int = 0, utf16_length: int = 0) -> str | None:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    data = bytes(raw)
    if compression_type == 1:
        # Snappy-compressed LSNG blob; decode if python-snappy is installed, else skip cleanly.
        try:
            import snappy  # type: ignore[import-not-found]

            data = snappy.decompress(data)
        except Exception:
            return None
    if utf16_length and len(data) == utf16_length * 2:
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-16-le", errors="replace")


def _origin_matches(origin: str, targets: Sequence[str]) -> bool:
    if not targets:
        return True
    low = origin.lower()
    host = low.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return _domain_matches(host, targets) or any(str(t).lower() in low for t in targets)


def extract_firefox_localstorage(
    profile_dir: Path,
    origins: Sequence[str] = (),
) -> dict[str, dict[str, str]]:
    """Extract ``localStorage`` key/value maps from Firefox (legacy + LSNG)."""
    profile_dir = Path(profile_dir).expanduser()
    result: dict[str, dict[str, str]] = {}

    with tempfile.TemporaryDirectory(prefix="unidl-ff-ls-") as tmp:
        tmp_path = Path(tmp)
        # 1) Legacy webappsstore.sqlite
        legacy_db = profile_dir / "webappsstore.sqlite"
        if legacy_db.is_file():
            snap = _snapshot_sqlite(legacy_db, tmp_path)
            conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT originKey, key, value FROM webappsstore2"
                ).fetchall()
                for origin_key, key, value in rows:
                    origin = _decode_webappsstore_origin(str(origin_key or ""))
                    if not _origin_matches(origin, origins):
                        continue
                    result.setdefault(origin, {})[str(key)] = str(value or "")
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        # 2) Modern LSNG storage/default/<origin>/ls/data.sqlite
        ls_root = profile_dir / "storage" / "default"
        if ls_root.is_dir():
            for idx, entry in enumerate(sorted(ls_root.iterdir(), key=lambda p: p.name)):
                if not entry.is_dir():
                    continue
                data_db = entry / "ls" / "data.sqlite"
                if not data_db.is_file():
                    continue
                origin = _decode_lsng_dir_origin(entry.name)
                if not _origin_matches(origin, origins):
                    continue
                sub_tmp = tmp_path / f"ls_{idx}"
                sub_tmp.mkdir(parents=True, exist_ok=True)
                snap = _snapshot_sqlite(data_db, sub_tmp)
                conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
                try:
                    cols = {
                        row[1].lower()
                        for row in conn.execute("PRAGMA table_info(data)").fetchall()
                    }
                    if "compression_type" in cols and "utf16_length" in cols:
                        rows = conn.execute(
                            "SELECT key, value, compression_type, utf16_length FROM data"
                        ).fetchall()
                    else:
                        rows = [
                            (r[0], r[1], 0, 0)
                            for r in conn.execute("SELECT key, value FROM data").fetchall()
                        ]
                    for key, raw_val, comp_type, u16_len in rows:
                        decoded = _decode_lsng_value(
                            raw_val,
                            compression_type=int(comp_type or 0),
                            utf16_length=int(u16_len or 0),
                        )
                        if decoded is not None:
                            result.setdefault(origin, {})[str(key)] = decoded
                except sqlite3.Error:
                    pass
                finally:
                    conn.close()

    return result


def extract_firefox_cookies_to_store(
    store: CookieStore,
    service_id: str,
    domains: Sequence[str],
    *,
    profile: str = "default",
    firefox_profile: Path | None = None,
    include_localstorage: bool = False,
) -> BrowserExtractResult:
    """Extract Firefox cookies for ``domains`` and persist them into ``CookieStore``."""
    if firefox_profile is None:
        profiles = discover_firefox_profiles()
        if not profiles:
            raise CookieError("No Firefox profiles found on this system.")
        chosen_profile = profiles[0].path
    else:
        chosen_profile = Path(firefox_profile).expanduser()

    jar = extract_firefox_cookies(chosen_profile, domains=domains)
    if not len(jar):
        dom_hint = ", ".join(domains) if domains else "any domain"
        raise CookieError(f"No Firefox cookies matched {dom_hint} in {chosen_profile.name}.")

    saved = store.save(jar, service_id, profile=profile)
    matched_domains = tuple(sorted({cookie.domain for cookie in jar if cookie.domain}))
    local_storage = (
        extract_firefox_localstorage(chosen_profile, origins=domains)
        if include_localstorage
        else {}
    )
    return BrowserExtractResult(
        profile=chosen_profile,
        cookies_count=len(jar),
        domains=matched_domains,
        saved_path=saved,
        local_storage=local_storage,
    )


__all__ = [
    "BrowserExtractResult",
    "FirefoxProfile",
    "default_firefox_roots",
    "discover_firefox_profiles",
    "extract_firefox_cookies",
    "extract_firefox_cookies_to_store",
    "extract_firefox_localstorage",
]

"""Signing in with browser cookies.

The third way into a service, alongside a username and password and a device
code. It matters more than it sounds: a lot of services have a sign-in flow that
cannot be reproduced from a script at all - a captcha, a device attestation, an
SSO redirect - and for those, exporting the cookies from a browser that is already
signed in is not a shortcut, it is the only route.

Following unshackle (``unshackle/commands/dl.py``, ``get_cookie_jar``):

* the format is **Netscape cookies.txt**, which is what every browser extension
  exports and what ``MozillaCookieJar`` reads
* an explicitly selected profile reads exactly
  ``<cookies>/<service>/<profile>.txt``. With no selection, only that service
  folder's ``default.txt`` is used
* **expiry is ignored on purpose.** A session cookie exported from a browser has
  no expiry, and a long-lived one is usually already past the date the exporter
  wrote. ``requests`` silently drops an expired cookie at request time, so a jar
  that loaded fine would send nothing and the service would answer as if signed
  out.

Deliberate deviation: unshackle **rewrites the user's file**, blanking the expiry
column in place, before loading it. That edits something the user exported and did
not ask us to touch, and it destroys the real expiry dates. Here the neutralising
happens on the in-memory jar instead (:func:`_keep_forever`), which has the same
effect on the wire and leaves the file alone.
"""

from __future__ import annotations

import html
import os
import tempfile
from dataclasses import dataclass
from http.cookiejar import CookieJar, MozillaCookieJar
from pathlib import Path

from .secureio import (
    FILE_MODE,
    atomic_write_via,
    locked_path,
    private_directory,
    private_file,
)

#: the first line every Netscape cookie file starts with, in one spelling or another
_HEADERS = ("# netscape http cookie file", "# http cookie file")


class CookieError(RuntimeError):
    """The file is not a cookie file, or cannot be read."""


@dataclass
class CookieInfo:
    """What is in a cookie file, for showing without leaking any values."""

    path: Path
    count: int = 0
    domains: tuple[str, ...] = ()
    profile: str = "default"

    def line(self) -> str:
        where = ", ".join(self.domains[:3])
        more = f" +{len(self.domains) - 3}" if len(self.domains) > 3 else ""
        return f"{self.count} cookie(s) for {where}{more}"


class CookieStore:
    """Finds, loads and saves one service's cookies."""

    def __init__(self, root: Path):
        self.root = Path(root)
        private_directory(self.root)

    @staticmethod
    def _component(value: str, *, label: str, default: str = "") -> str:
        """One path component, never a path into a neighbour's directory."""
        raw = str(value or "").strip() or default
        if (
            not raw
            or raw in {".", ".."}
            or "/" in raw
            or "\\" in raw
            or "\0" in raw
            or Path(raw).name != raw
        ):
            raise CookieError(f"invalid cookie {label}: {value!r}")
        return raw

    def _service_dir(self, service_id: str) -> Path:
        service = self._component(service_id, label="service")
        return self.root / service

    def profile_files(self, service_id: str) -> list[Path]:
        """Selectable cookie files owned by this service, newest state read live.

        Only direct, ordinary ``.txt`` files are returned. A symlink could point
        into another service's folder (or anywhere else), which would make the
        setting claim one boundary while reading across another.
        """
        folder = self._service_dir(service_id)
        if not folder.is_dir():
            return []
        try:
            found = [
                path
                for path in folder.iterdir()
                if path.suffix == ".txt" and path.is_file() and not path.is_symlink()
            ]
        except OSError:
            return []
        return sorted(found, key=lambda path: path.name.casefold())

    # ----------------------------------------------------------- locating
    def candidates(
        self,
        service_id: str,
        profile: str = "",
        *,
        legacy_service_ids: tuple[str, ...] | list[str] = (),
    ) -> list[Path]:
        """Every permitted location for this selection, best first.

        A named selection is strict: choosing ``work.txt`` must never silently
        authenticate as ``default.txt``. An automatic selection is strict about
        ownership too: it reads this service's ``default.txt`` and nothing beside
        another service's directory.
        """
        selected = str(profile or "").strip()
        candidates: list[Path] = []
        for raw_service in (service_id, *legacy_service_ids):
            service = self._component(raw_service, label="service")
            if selected:
                wanted = self._component(selected, label="profile")
                candidate = self.root / service / f"{wanted}.txt"
            else:
                candidate = self.root / service / "default.txt"
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def path_for(
        self,
        service_id: str,
        profile: str = "",
        *,
        legacy_service_ids: tuple[str, ...] | list[str] = (),
    ) -> Path | None:
        """The cookie file in use, or None when there is not one."""
        for candidate in self.candidates(
            service_id, profile, legacy_service_ids=legacy_service_ids
        ):
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    def target(self, service_id: str, profile: str = "") -> Path:
        """Where a newly imported file goes.

        The per-service folder rather than the flat ``<service>.txt``, because a
        second account should not have to move the first one out of the way.
        """
        service = self._component(service_id, label="service")
        wanted = self._component(profile, label="profile", default="default")
        return self.root / service / f"{wanted}.txt"

    def has(
        self,
        service_id: str,
        profile: str = "",
        *,
        legacy_service_ids: tuple[str, ...] | list[str] = (),
    ) -> bool:
        return self.path_for(
            service_id, profile, legacy_service_ids=legacy_service_ids
        ) is not None

    # ------------------------------------------------------------ reading
    def jar_for(
        self,
        service_id: str,
        profile: str = "",
        *,
        legacy_service_ids: tuple[str, ...] | list[str] = (),
    ) -> MozillaCookieJar | None:
        """The service's cookies, ready to attach to a session."""
        path = self.path_for(
            service_id, profile, legacy_service_ids=legacy_service_ids
        )
        return self.load(path) if path is not None else None

    def load(self, path: Path) -> MozillaCookieJar:
        """Read one cookie file.

        HTML entities are unescaped first: an exporter that has been through a web
        page writes ``&amp;`` inside a value, and a cookie value with a literal
        ``&amp;`` in it is simply the wrong cookie.
        """
        path = Path(path)
        if not path.is_file():
            raise CookieError(f"no cookie file at {path}")
        private_file(path)
        try:
            text = html.unescape(path.read_text("utf-8", errors="replace"))
        except OSError as exc:
            raise CookieError(f"could not read {path.name}: {exc}") from exc
        text = _with_netscape_header(text)

        jar = MozillaCookieJar()
        # Parsed from a temporary copy rather than the original: MozillaCookieJar
        # only reads from a file, and unescaping means what is loaded is no longer
        # byte-identical to what is on disk.
        private_directory(self.root)
        descriptor, scratch_name = tempfile.mkstemp(
            prefix=".cookie-read.", suffix=".txt", dir=str(self.root)
        )
        scratch = Path(scratch_name)
        try:
            os.fchmod(descriptor, FILE_MODE)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            jar.filename = str(scratch)
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as exc:  # cookiejar raises LoadError, not OSError
            raise CookieError(_why(path, text, exc)) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            scratch.unlink(missing_ok=True)
            jar.filename = str(path)

        if not len(jar):
            raise CookieError(f"{path.name} has no cookies in it")
        _keep_forever(jar)
        return jar

    def info(
        self,
        service_id: str,
        profile: str = "",
        *,
        legacy_service_ids: tuple[str, ...] | list[str] = (),
    ) -> CookieInfo | None:
        """A description of the file in use, with no values in it."""
        path = self.path_for(
            service_id, profile, legacy_service_ids=legacy_service_ids
        )
        if path is None:
            return None
        try:
            jar = self.load(path)
        except CookieError:
            return CookieInfo(path=path, count=0, profile=profile or "default")
        domains = sorted({cookie.domain for cookie in jar if cookie.domain})
        return CookieInfo(
            path=path,
            count=len(jar),
            domains=tuple(domains),
            profile=profile or "default",
        )

    # ------------------------------------------------------------ writing
    def import_file(self, source: Path, service_id: str, profile: str = "") -> Path:
        """Copy a cookie file into place, after checking it is one.

        Validated before it is copied, so a wrong path or an HTML error page saved
        with a ``.txt`` name fails here - where it can be explained - rather than
        as a service answering "signed out" later.
        """
        source = Path(source).expanduser()
        jar = self.load(source)  # raises CookieError with a usable message
        destination = self.target(service_id, profile)
        private_directory(destination.parent)
        # Written through the jar rather than copied byte for byte: it normalises
        # the file, and it proves once more that what landed can be read back.
        with locked_path(destination):
            atomic_write_via(
                destination,
                lambda temporary: _save_jar(jar, temporary),
            )
        jar.filename = str(destination)
        return destination

    def save(
        self,
        jar: CookieJar,
        service_id: str,
        profile: str = "",
        *,
        legacy_service_ids: tuple[str, ...] | list[str] = (),
    ) -> Path | None:
        """Write a session's cookies back, so a refreshed session survives.

        Merged into what is already on disk rather than replacing it: a session
        only carries the cookies for the hosts it happened to talk to, and
        overwriting would drop the rest of the account's.
        """
        # Refreshes always write the canonical folder.  If the only existing
        # jar is under a legacy id, merge it into the new destination once
        # instead of extending the old namespace forever.
        existing = self.path_for(
            service_id, profile, legacy_service_ids=legacy_service_ids
        )
        path = self.target(service_id, profile)
        private_directory(path.parent)
        try:
            with locked_path(path):
                merged = MozillaCookieJar()
                if path.is_file():
                    try:
                        for cookie in self.load(path):
                            merged.set_cookie(cookie)
                    except CookieError:
                        pass
                if existing is not None and existing != path:
                    try:
                        for cookie in self.load(existing):
                            merged.set_cookie(cookie)
                    except CookieError:
                        pass
                for cookie in _as_jar(jar):
                    merged.set_cookie(cookie)
                if not len(merged):
                    return None
                atomic_write_via(path, lambda temporary: _save_jar(merged, temporary))
        except OSError:
            return None
        if existing is not None and existing != path and existing.is_file() and not existing.is_symlink():
            try:
                with locked_path(existing):
                    existing.unlink()
            except OSError:
                pass
        return path

    def remove(
        self,
        service_id: str,
        profile: str = "",
        *,
        legacy_service_ids: tuple[str, ...] | list[str] = (),
    ) -> bool:
        """Delete the cookie file in use. Returns whether there was one."""
        removed = False
        # A logout must clear both names. Otherwise an old file can silently
        # revive the session after the service id was renamed.
        for path in self.candidates(
            service_id, profile, legacy_service_ids=legacy_service_ids
        ):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                with locked_path(path):
                    path.unlink()
            except OSError:
                continue
            removed = True
        return removed

    def directory_for(self, service_id: str) -> Path:
        path = self._service_dir(service_id)
        return private_directory(path)


# ------------------------------------------------------------------ helpers


def _save_jar(jar: MozillaCookieJar, path: Path) -> None:
    jar.filename = str(path)
    jar.save(ignore_discard=True, ignore_expires=True)


def _keep_forever(jar: CookieJar) -> None:
    """Stop ``requests`` from dropping a cookie for being out of date.

    ``Cookie.is_expired`` is what does the dropping, and it answers False for an
    expiry of ``None``. Loading with ``ignore_expires=True`` only gets a cookie
    *into* the jar; it does not get it onto the wire.
    """
    for cookie in jar:
        cookie.expires = None
        cookie.discard = False


def _as_jar(cookies: CookieJar):
    """Iterate cookies from a jar or from a requests cookie jar."""
    jar = getattr(cookies, "jar", cookies)
    return list(jar)


def _with_netscape_header(text: str) -> str:
    """Add the optional Netscape heading to an otherwise valid seven-column export.

    Some browser exporters write the tab-separated cookie rows without the
    comment line MozillaCookieJar insists on.  Recognise only the complete
    Netscape shape here; malformed text still reaches :func:`_why` unchanged and
    gets its usual useful error.  This operates on the temporary in-memory copy,
    never on the file the user exported.
    """
    first = next((line for line in text.splitlines() if line.strip()), "")
    lowered = first.strip().lower()
    if any(lowered.startswith(header) for header in _HEADERS):
        return text

    rows = []
    for raw_line in text.splitlines():
        line = raw_line.strip("\r\n")
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        columns = line.split("\t")
        if len(columns) != 7:
            return text
        domain, include_subdomains, cookie_path, secure, expires, name, _value = columns
        if domain.startswith("#HttpOnly_"):
            domain = domain.removeprefix("#HttpOnly_")
        if (
            not domain
            or include_subdomains.upper() not in {"TRUE", "FALSE"}
            or not cookie_path.startswith("/")
            or secure.upper() not in {"TRUE", "FALSE"}
            or (expires and not expires.isdigit())
            or not name
        ):
            return text
        rows.append(columns)

    if not rows:
        return text
    return "# Netscape HTTP Cookie File\n" + text


def _why(path: Path, text: str, exc: Exception) -> str:
    """Say what is wrong with a file that would not parse.

    "invalid Netscape format cookies file" is true and useless. The three things
    that actually happen are a missing header line, a file that is HTML, and a
    file whose columns are spaces instead of tabs - and each has a different fix.
    """
    first = next((line for line in text.splitlines() if line.strip()), "")
    lowered = first.strip().lower()
    if lowered.startswith("<"):
        return (
            f"{path.name} is a web page, not a cookie file - the export probably "
            "saved the page instead of the download"
        )
    if not any(lowered.startswith(header) for header in _HEADERS):
        return (
            f"{path.name} does not start with the Netscape cookie header. Export "
            "it again as cookies.txt, or add this as its first line:\n"
            "    # Netscape HTTP Cookie File"
        )
    if "\t" not in text:
        return (
            f"{path.name} has no tab characters, so its columns cannot be read - "
            "something has reformatted it (a spreadsheet, or copy and paste)"
        )
    return f"{path.name} could not be read as a cookie file: {exc}"


def cookie_names(jar: CookieJar) -> list[str]:
    """The cookie names in a jar. Names only - never the values."""
    return sorted({cookie.name for cookie in _as_jar(jar)})


def as_header(jar: CookieJar, domain_hint: str = "") -> str:
    """A ``Cookie:`` header, for handing a session to the downloader.

    UniDL gets headers, not a jar, so a service whose *media* is behind the
    same session as its API has to send them this way. Narrowed by domain where
    one is given, because sending an account's whole jar to a CDN is both
    unnecessary and a way to trip a request-size limit.
    """
    hint = (domain_hint or "").lower().lstrip(".")
    pairs: dict[str, str] = {}
    for cookie in _as_jar(jar):
        domain = (cookie.domain or "").lower().lstrip(".")
        if hint and domain and not (hint == domain or hint.endswith(f".{domain}")):
            continue
        if cookie.name:
            pairs[cookie.name] = cookie.value or ""
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


__all__ = [
    "CookieError",
    "CookieInfo",
    "CookieStore",
    "as_header",
    "cookie_names",
]

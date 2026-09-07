"""Best-effort release discovery for the Home screen.

The checker deliberately has no authority to install anything.  It reads the
public PyPI metadata (the installable distribution) and the public GitHub
release metadata (human-readable notes and a browser link), then hands a small
immutable result to the TUI.  Network failures are normal here: an offline
start must never make the application look broken.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .. import __version__

PYPI_JSON_URL = "https://pypi.org/pypi/unidl/json"
PYPI_PROJECT_URL = "https://pypi.org/project/unidl/"
GITHUB_REPOSITORY_URL = "https://github.com/chis47747/unidl"
GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/chis47747/unidl/releases/latest"
DEFAULT_TIMEOUT = 4.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_RELEASE_NOTES = 12_000


@dataclass(frozen=True)
class UpdateInfo:
    """The public release data needed by the Home and update screens."""

    current_version: str
    latest_version: str | None = None
    pypi_version: str | None = None
    github_version: str | None = None
    pypi_url: str = PYPI_PROJECT_URL
    github_url: str = GITHUB_REPOSITORY_URL
    release_notes: str = ""
    release_title: str = ""
    checked: bool = False
    error: str | None = None

    @property
    def update_available(self) -> bool:
        return bool(self.latest_version and _version_key(self.latest_version) > _version_key(self.current_version))

    @property
    def current(self) -> bool:
        return self.checked and not self.error and not self.update_available


def _version_key(value: str) -> tuple[tuple[int, ...], int, str]:
    """Return a conservative comparable key without adding ``packaging``.

    Public releases are ordinary PEP 440 versions, but a GitHub tag can have a
    leading ``v`` or a suffix.  Numeric release segments are compared first;
    prereleases sort before a final release and unknown suffixes remain stable.
    """

    text = str(value or "").strip().lstrip("vV")
    match = re.match(r"(\d+(?:\.\d+)*)(.*)$", text)
    if not match:
        return ((-1,), 0, text.lower())
    numbers = tuple(int(part) for part in match.group(1).split("."))
    suffix = match.group(2).strip().lower()
    return numbers, (0 if suffix else 1), suffix


def _json_get(url: str, *, timeout: float) -> dict | list:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"UniDL/{__version__} update-check",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS URLs above
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("response was too large")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, (dict, list)):
        raise ValueError("response was not a JSON object")
    return data


def _github_release(data: dict | list) -> tuple[str | None, str, str, str]:
    if not isinstance(data, dict):
        return None, "", "", GITHUB_REPOSITORY_URL
    tag = str(data.get("tag_name") or "").strip()
    version = tag.lstrip("vV") or None
    if version and _version_key(version)[0] == (-1,):
        version = None
    title = str(data.get("name") or tag or "").strip()
    notes = str(data.get("body") or "").strip()[:MAX_RELEASE_NOTES]
    url = str(data.get("html_url") or GITHUB_REPOSITORY_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {"github.com", "www.github.com"}:
        url = GITHUB_REPOSITORY_URL
    return version, title, notes, url


@lru_cache(maxsize=1)
def check_for_updates(timeout: float = DEFAULT_TIMEOUT) -> UpdateInfo:
    """Read PyPI and GitHub once per process and compare with this build.

    The cache prevents opening the update modal or rebuilding the Home screen
    from issuing another request during one run.  A new process naturally gets
    a fresh check, while every individual request remains bounded by ``timeout``.
    """

    pypi_version: str | None = None
    pypi_url = PYPI_PROJECT_URL
    github_version: str | None = None
    github_url = GITHUB_REPOSITORY_URL
    release_title = ""
    release_notes = ""
    failures: list[str] = []

    try:
        pypi_data = _json_get(PYPI_JSON_URL, timeout=timeout)
        if isinstance(pypi_data, dict):
            info = pypi_data.get("info")
            if isinstance(info, dict):
                candidate = str(info.get("version") or "").strip()
                if candidate:
                    pypi_version = candidate
                project_urls = info.get("project_urls")
                if isinstance(project_urls, dict):
                    candidate_url = str(project_urls.get("Homepage") or project_urls.get("Repository") or "").strip()
                    if candidate_url.startswith("https://"):
                        pypi_url = PYPI_PROJECT_URL
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"PyPI: {type(exc).__name__}")

    try:
        github_data = _json_get(GITHUB_LATEST_RELEASE_URL, timeout=timeout)
        github_version, release_title, release_notes, github_url = _github_release(github_data)
    except HTTPError as exc:
        # A repository without a GitHub Release is valid; PyPI remains the
        # install source and the repository link is still useful to the user.
        if exc.code != 404:
            failures.append(f"GitHub: HTTP {exc.code}")
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"GitHub: {type(exc).__name__}")

    candidates = [version for version in (pypi_version, github_version) if version]
    latest = max(candidates, key=_version_key) if candidates else None
    return UpdateInfo(
        current_version=__version__,
        latest_version=latest,
        pypi_version=pypi_version,
        github_version=github_version,
        pypi_url=pypi_url,
        github_url=github_url,
        release_notes=release_notes,
        release_title=release_title,
        checked=bool(candidates),
        error="; ".join(failures) if failures else ("No release metadata" if not candidates else None),
    )


def clear_update_cache() -> None:
    """Clear the process cache, primarily for tests and an explicit refresh."""

    check_for_updates.cache_clear()


__all__ = [
    "DEFAULT_TIMEOUT",
    "GITHUB_LATEST_RELEASE_URL",
    "GITHUB_REPOSITORY_URL",
    "PYPI_JSON_URL",
    "PYPI_PROJECT_URL",
    "UpdateInfo",
    "check_for_updates",
    "clear_update_cache",
]

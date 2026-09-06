"""Provider-facing HTTP and parsing scaffold for the example service.

The URLs and JSON field names are intentionally placeholders.  This module is
safe to import because it never creates a session or performs a request at
import time.  Replace the endpoint paths and parsers with the provider's
documented API, then keep those details here instead of in Flow/UI code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NoReturn
from urllib.parse import quote, urljoin, urlparse

import requests

EXAMPLE_ORIGIN = "https://example.invalid/"
USER_AGENT = "UniDL-example/1.0"


class ExampleApiError(RuntimeError):
    """A provider request or response could not produce a usable result."""


@dataclass(frozen=True)
class ExampleItem:
    """Normalized provider item consumed by the service Flow."""

    id: str
    title: str
    manifest_url: str = ""
    license_url: str = ""
    year: str | None = None
    kind: str = "movie"
    season: int | None = None
    episode: int | None = None
    episode_name: str | None = None
    encrypted: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    license_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> ExampleItem:
        """Validate one provider object without exposing raw JSON to Core."""
        if not isinstance(payload, dict):
            raise ExampleApiError("Example API returned a non-object title")
        identifier = str(payload.get("id") or payload.get("content_id") or "").strip()
        title = str(payload.get("title") or payload.get("name") or "").strip()
        if not identifier or not title:
            raise ExampleApiError("Example API returned a title without id or name")

        def optional_int(value: Any) -> int | None:
            if value in (None, ""):
                return None
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ExampleApiError(f"invalid numeric field for Example title {identifier!r}") from exc

        def string_map(value: Any) -> dict[str, str]:
            if not isinstance(value, dict):
                return {}
            return {str(key): str(item) for key, item in value.items() if item is not None}

        return cls(
            id=identifier,
            title=title,
            manifest_url=str(payload.get("manifest_url") or payload.get("manifest") or ""),
            license_url=str(payload.get("license_url") or payload.get("license") or ""),
            year=str(payload["year"]) if payload.get("year") not in (None, "") else None,
            kind=str(payload.get("kind") or "movie").lower(),
            season=optional_int(payload.get("season")),
            episode=optional_int(payload.get("episode")),
            episode_name=(
                str(payload["episode_name"])
                if payload.get("episode_name") not in (None, "")
                else None
            ),
            encrypted=bool(payload.get("encrypted", bool(payload.get("license_url")))),
            headers=string_map(payload.get("headers")),
            license_headers=string_map(payload.get("license_headers")),
        )


class ExampleApi:
    """Small requests client to be adapted for a real provider.

    ``requests.Session`` is injected so tests can provide a deterministic fake
    and the service can use Core's proxy/cookie-aware session.  No credentials,
    token or local path is embedded in this template.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        base_url: str = EXAMPLE_ORIGIN,
        timeout: float = 30.0,
    ):
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return path if urlparse(path).scheme else urljoin(self.base_url, path.lstrip("/"))

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> dict[str, Any]:
        """Make one checked request; provider auth belongs in this method."""
        try:
            response = self.session.request(
                method,
                self._url(path),
                params=params,
                headers=headers,
                json=body,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ExampleApiError(f"Example API request failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExampleApiError("Example API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ExampleApiError("Example API returned a JSON value instead of an object")
        return payload

    def search(self, query: str) -> list[ExampleItem]:
        """Return normalized search results; adjust the endpoint for the provider."""
        query = str(query or "").strip()
        if not query:
            return []
        payload = self._request_json("GET", "api/search", params={"q": query})
        values = payload.get("items", payload.get("results", []))
        if not isinstance(values, list):
            raise ExampleApiError("Example search response has no items list")
        return [ExampleItem.from_payload(value) for value in values]

    def resolve(self, target: str) -> ExampleItem | None:
        """Resolve a provider URL or ID into a normalized playback item."""
        raw = str(target or "").strip()
        if not raw:
            return None
        parsed = urlparse(raw)
        identifier = parsed.path.rstrip("/").rsplit("/", 1)[-1] if parsed.scheme else raw
        if not identifier:
            return None
        payload = self._request_json("GET", f"api/titles/{quote(identifier, safe='')}")
        value = payload.get("title", payload)
        return ExampleItem.from_payload(value)

    def post_license(
        self,
        license_url: str,
        challenge: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Post a Widevine challenge using the provider's exact contract.

        Adapt the content type, body encoding and response handling to the
        provider.  Do not replace this with a shared generic license request.
        """
        request_headers = {"Content-Type": "application/octet-stream"}
        request_headers.update(headers or {})
        try:
            response = self.session.post(
                self._url(license_url),
                data=challenge,
                headers=request_headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ExampleApiError(f"Example license request failed: {exc}") from exc
        if not response.content:
            raise ExampleApiError("Example license response was empty")
        return response.content

    def not_implemented(self, operation: str) -> NoReturn:
        """Make an unfinished provider operation fail with an actionable hint."""
        raise ExampleApiError(
            f"ExampleApi.{operation} is a template operation; replace the example endpoint and parser"
        )


__all__ = ["EXAMPLE_ORIGIN", "ExampleApi", "ExampleApiError", "ExampleItem"]

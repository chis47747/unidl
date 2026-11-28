"""Gluetun Docker VPN HTTP proxy & Control Server integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .proxy import ProxyResolutionError

_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "us": ("us", "usa", "united states", "united states of america"),
    "uk": ("gb", "uk", "united kingdom", "great britain"),
    "gb": ("gb", "uk", "united kingdom", "great britain"),
    "jp": ("jp", "japan"),
    "kr": ("kr", "south korea", "korea"),
    "tw": ("tw", "taiwan"),
    "hk": ("hk", "hong kong"),
    "sg": ("sg", "singapore"),
    "de": ("de", "germany"),
    "fr": ("fr", "france"),
    "ca": ("ca", "canada"),
    "au": ("au", "australia"),
    "nl": ("nl", "netherlands"),
    "ch": ("ch", "switzerland"),
    "se": ("se", "sweden"),
    "no": ("no", "norway"),
    "es": ("es", "spain"),
    "it": ("it", "italy"),
    "br": ("br", "brazil"),
    "mx": ("mx", "mexico"),
    "in": ("in", "india"),
    "ie": ("ie", "ireland"),
    "nz": ("nz", "new zealand"),
    "pl": ("pl", "poland"),
}


def _country_matches(actual: str, expected_code: str) -> bool:
    act = str(actual or "").strip().lower()
    exp = str(expected_code or "").strip().lower()
    if not act or not exp:
        return True
    aliases = _COUNTRY_ALIASES.get(exp, (exp,))
    return act in aliases or act == exp


class GluetunClient:
    """Resolve a Gluetun container HTTP proxy and optionally verify its exit country."""

    def __init__(self, spec: Mapping[str, Any]):
        self.spec = dict(spec)

    def _base_proxy_uri(self) -> str:
        raw_url = str(self.spec.get("url") or "").strip()
        if raw_url:
            if "://" not in raw_url:
                raw_url = f"http://{raw_url}"
            parsed = urlsplit(raw_url)
            if not parsed.hostname:
                raise ProxyResolutionError(f"Gluetun proxy URL has no hostname: {raw_url!r}")
            return raw_url
        host = str(self.spec.get("host") or "127.0.0.1").strip()
        port = int(self.spec.get("port") or 8888)
        scheme = str(self.spec.get("scheme") or "http").strip().lower() or "http"
        user = str(self.spec.get("username") or "").strip()
        pwd = str(self.spec.get("password") or "").strip()
        auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user and pwd else ""
        return f"{scheme}://{auth}{host}:{port}"

    def _control_url(self) -> str:
        explicit = str(self.spec.get("control_url") or "").strip().rstrip("/")
        if explicit:
            return explicit
        host = str(self.spec.get("host") or "").strip()
        if not host:
            parsed = urlsplit(self._base_proxy_uri())
            host = parsed.hostname or "127.0.0.1"
        control_port = int(self.spec.get("control_port") or 8000)
        return f"http://{host}:{control_port}"

    def public_ip_info(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Query Gluetun's Control Server ``/v1/publicip/ip`` endpoint."""
        url = f"{self._control_url()}/v1/publicip/ip"
        headers = {"Accept": "application/json"}
        api_key = str(self.spec.get("api_key") or "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise ProxyResolutionError(f"Gluetun control server request failed: {exc}") from exc
        except ValueError as exc:
            raise ProxyResolutionError("Gluetun control server returned invalid JSON") from exc
        return dict(data) if isinstance(data, Mapping) else {}

    def proxy(self, query: str = "") -> str | None:
        """Return the Gluetun HTTP proxy URI, checking region map or active exit country."""
        region = str(query or "").strip().lower().partition(":")[0]
        # Allow per-region container map: e.g. instances: {us: "http://gluetun-us:8888"}
        instances = self.spec.get("instances") or self.spec.get("server_map") or {}
        if region and isinstance(instances, Mapping):
            lowered = {str(k).strip().lower(): str(v).strip() for k, v in instances.items() if str(v or "").strip()}
            if region in lowered:
                uri = lowered[region]
                return uri if "://" in uri else f"http://{uri}"
            if self.spec.get("instances") and not self.spec.get("url") and not self.spec.get("host"):
                return None

        uri = self._base_proxy_uri()
        verify_region = bool(self.spec.get("verify_region", False))
        if verify_region and region and len(region) == 2 and region.isalpha():
            info = self.public_ip_info()
            actual = str(info.get("country_code") or info.get("country") or "").strip()
            if actual and not _country_matches(actual, region):
                raise ProxyResolutionError(
                    f"Gluetun exit country is {actual.upper()}, expected {region.upper()}"
                )
        return uri


__all__ = ["GluetunClient"]

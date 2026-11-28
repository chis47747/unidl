"""Proton VPN proxy provider (country, city, server-number and cached tokens)."""

from __future__ import annotations

import json
import random
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .proxy import ProxyResolutionError

_LOGICALS_URL = "https://api.protonvpn.ch/vpn/logicals"
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "UniDL/1.0",
}


class ProtonVPNClient:
    """Resolve Proton VPN proxy endpoints by country, city or server number."""

    def __init__(self, spec: Mapping[str, Any], token_root: Path | None = None):
        self.spec = dict(spec)
        self.token_root = Path(token_root) if token_root is not None else Path.home() / ".unidl" / "tokens" / "vpn"

    @property
    def token_file(self) -> Path:
        return self.token_root / "protonvpn_tokens.json"

    def _load_cached_credentials(self) -> tuple[str, str]:
        if not self.token_file.is_file():
            return "", ""
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "", ""
        if not isinstance(data, Mapping):
            return "", ""
        user = str(data.get("username") or data.get("proxy_username") or "").strip()
        pwd = str(data.get("password") or data.get("proxy_password") or "").strip()
        return user, pwd

    def _credentials(self) -> tuple[str, str]:
        username = str(self.spec.get("username") or "").strip()
        password = str(self.spec.get("password") or "").strip()
        if not username or not password:
            cached_user, cached_pwd = self._load_cached_credentials()
            username = username or cached_user
            password = password or cached_pwd
        if not username or not password:
            raise ProxyResolutionError("Proton VPN needs service username and password (or cached token credentials)")
        return quote(username, safe=""), quote(password, safe="")

    def _server_map(self) -> dict[str, Any]:
        raw = self.spec.get("server_map") or {}
        return {str(k).strip().lower(): v for k, v in raw.items()} if isinstance(raw, Mapping) else {}

    def _fetch_logicals(self) -> list[Mapping[str, Any]]:
        url = str(self.spec.get("logicals_url") or _LOGICALS_URL).strip()
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            raise ProxyResolutionError(f"Proton VPN server lookup failed: {exc}") from exc
        except ValueError as exc:
            raise ProxyResolutionError("Proton VPN returned invalid server JSON") from exc
        logicals = payload.get("LogicalServers", []) if isinstance(payload, Mapping) else payload
        return [item for item in logicals if isinstance(item, Mapping)] if isinstance(logicals, list) else []

    def proxy(self, query: str) -> str | None:
        username, password = self._credentials()
        scheme = str(self.spec.get("scheme") or "https").strip().lower() or "https"
        port = int(self.spec.get("port") or (443 if scheme == "https" else 1080))
        region, sep, city = str(query or "").strip().lower().partition(":")
        city = city if sep else ""
        if not region:
            raise ProxyResolutionError("Proton VPN query requires a country code (e.g. us, jp, us:new_york, us12)")

        mapped = self._server_map().get(f"{region}:{city}" if city else region)
        if mapped:
            host = str(mapped).strip()
            if "://" in host:
                return host
            return f"{scheme}://{username}:{password}@{host}:{port}"

        wanted_num = ""
        numbered = re.fullmatch(r"([a-z]{2})[#-]?(\d+)", region)
        if numbered:
            region, wanted_num = numbered.groups()

        logicals = self._fetch_logicals()
        candidates: list[tuple[int, str]] = []
        for server in logicals:
            if int(server.get("Status", 1) or 0) != 1:
                continue
            exit_cc = str(server.get("ExitCountry") or server.get("EntryCountry") or "").strip().lower()
            if exit_cc != region:
                continue
            if city:
                server_city = str(server.get("City") or "").strip().lower().replace(" ", "_")
                wanted_city = city.replace(" ", "_").replace("-", "_")
                if server_city != wanted_city and str(server.get("City") or "").strip().lower() != city.replace("_", " "):
                    continue
            name = str(server.get("Name") or "").strip()
            if wanted_num:
                # Match names like "US-NY#12", "US#12", "US-FREE#12"
                num_match = re.search(r"#(\d+)$", name)
                if not num_match or int(num_match.group(1)) != int(wanted_num):
                    continue
            domain = str(server.get("Domain") or "").strip()
            if not domain:
                servers_list = server.get("Servers") or []
                if isinstance(servers_list, list) and servers_list and isinstance(servers_list[0], Mapping):
                    domain = str(servers_list[0].get("Domain") or servers_list[0].get("EntryIP") or "").strip()
            if not domain:
                continue
            load = int(server.get("Load", 50) or 50)
            candidates.append((load, domain))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        top_pool = [host for _load, host in candidates[:5]]
        hostname = random.choice(top_pool)
        return f"{scheme}://{username}:{password}@{hostname}:{port}"


__all__ = ["ProtonVPNClient"]

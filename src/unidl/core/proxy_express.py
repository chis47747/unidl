"""ExpressVPN HTTPS proxy provider with explicit TV device authorization.

This follows ExpressVPN's Android TV contract used by unshackle: OAuth device
authorization obtains refreshable account tokens, a subscription receipt lists
proxy-capable locations, and a connection token authenticates the final HTTPS
proxy as ``cat:<token>``.  Interactive authorization is kept outside this core
client so the TUI can display/copy the code and cancel polling cleanly.
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .proxy import ProxyResolutionError
from .secureio import atomic_write_text, locked_path, private_directory


@dataclass
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int


class ExpressVPNAuthorizationRequired(ProxyResolutionError):
    """ExpressVPN has no reusable account session and needs device authorization."""


class ExpressVPNClient:
    CLIENT_ID = "8V18PnFYlrYnnYvRlnxKwifxxhYKfjIG"
    AUTH_BASE = "https://auth.expressvpn.com/oauth"
    API_BASE = "https://cp.expressapisv2.net"
    SCOPE = "openid profile email offline_access"
    DEVICE_POLL_TIMEOUT = 600
    USER_AGENT = "okhttp/5.3.2"

    def __init__(self, spec: Mapping[str, Any], token_root: Path) -> None:
        self.spec = dict(spec)
        self.region_map = _mapping(spec.get("region_map"), allow_empty=True)
        self.server_map = _mapping(spec.get("server_map"))
        self.refresh_token = _text(spec.get("refresh_token"))
        self.access_token = _text(spec.get("access_token"))
        self.connection_token = _text(spec.get("connection_token"))
        account_json = _text(spec.get("account_json"))
        self.account_json = Path(account_json).expanduser() if account_json else None
        try:
            self.timeout = max(2.0, min(float(spec.get("timeout") or 10), 60.0))
        except (TypeError, ValueError):
            self.timeout = 10.0

        configured_cache = _text(spec.get("cache_path"))
        self.cache_path = (
            Path(configured_cache).expanduser()
            if configured_cache
            else Path(token_root) / "expressvpn_tokens.json"
        )
        self.tokens: dict[str, Any] | None = None
        self.srt: str | None = None
        self.locations: list[dict[str, Any]] | None = None
        self.last_name: str | None = None
        self.last_index: int | None = None
        self.last_total: int | None = None
        self.last_host: str | None = None

    # ------------------------------------------------------------ authorization
    def has_silent_session(self) -> bool:
        return bool(
            self.tokens
            or self.access_token
            or self.refresh_token
            or self.connection_token
            or (self.account_json and self.account_json.is_file())
            or self.load_cache()
        )

    def request_device_authorization(self) -> DeviceAuthorization:
        response = self.request(
            "POST",
            f"{self.AUTH_BASE}/device/code",
            data={"client_id": self.CLIENT_ID, "scope": self.SCOPE, "audience": ""},
            headers=self.form_headers(),
            allow_error=True,
        )
        data = _response_json(response, "ExpressVPN device authorization")
        if not response.ok:
            raise ProxyResolutionError(
                f"ExpressVPN device authorization failed with HTTP {response.status_code}"
            )
        device_code = _text(data.get("device_code"))
        user_code = _text(data.get("user_code"))
        if not device_code or not user_code:
            raise ProxyResolutionError("ExpressVPN device authorization returned no code")
        try:
            expires_in = int(data.get("expires_in") or self.DEVICE_POLL_TIMEOUT)
            interval = max(1, min(int(data.get("interval") or 5), 30))
        except (TypeError, ValueError):
            expires_in, interval = self.DEVICE_POLL_TIMEOUT, 5
        return DeviceAuthorization(
            device_code=device_code,
            user_code=user_code,
            verification_uri=_text(data.get("verification_uri"))
            or "https://auth.expressvpn.com/realms/xvpn/device",
            expires_at=time.time() + expires_in,
            interval=interval,
        )

    def poll_device_authorization(self, authorization: DeviceAuthorization) -> dict[str, Any] | None:
        response = self.request(
            "POST",
            f"{self.AUTH_BASE}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.CLIENT_ID,
                "device_code": authorization.device_code,
            },
            headers=self.form_headers(),
            allow_error=True,
        )
        data = _response_json(response, "ExpressVPN device authorization poll")
        if response.ok:
            if not data.get("access_token"):
                raise ProxyResolutionError("ExpressVPN approved the device without returning an access token")
            return data
        error = _text(data.get("error"))
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            authorization.interval = min(authorization.interval + 5, 30)
            return None
        if error in {"access_denied", "expired_token"}:
            raise ProxyResolutionError(f"ExpressVPN device authorization ended: {error.replace('_', ' ')}")
        raise ProxyResolutionError(
            f"ExpressVPN device authorization failed: {error or f'HTTP {response.status_code}'}"
        )

    def save_device_tokens(self, tokens: Mapping[str, Any]) -> None:
        current = self.load_cache()
        current.update({str(key): value for key, value in tokens.items() if value})
        self.save_cache(current)

    # ------------------------------------------------------------------ tokens
    def get_tokens(self) -> dict[str, Any]:
        if self.tokens and self.tokens.get("access_token") and not jwt_expired(self.tokens.get("access_token")):
            return self.tokens

        overrides = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "connection_token": self.connection_token,
        }
        tokens = {**self.load_cache(), **{key: value for key, value in overrides.items() if value}}
        if tokens.get("access_token") and not jwt_expired(tokens.get("access_token")):
            self.tokens = tokens
            return tokens

        if refresh_token := _text(tokens.get("refresh_token")):
            refreshed = self.refresh_access_token(refresh_token)
            if refreshed:
                tokens["access_token"] = refreshed.get("access_token")
                tokens["refresh_token"] = refreshed.get("refresh_token") or refresh_token
                self.save_cache(tokens)
                return self.tokens or tokens

        if self.account_json and not self.account_json.is_symlink() and self.account_json.is_file():
            try:
                data = json.loads(self.account_json.read_text("utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProxyResolutionError(f"Could not read ExpressVPN account JSON: {exc}") from exc
            if isinstance(data, Mapping):
                tokens.update(
                    access_token=data.get("accessToken"),
                    connection_token=data.get("connectionToken"),
                    subscription_id=data.get("subscriptionId"),
                )
                self.tokens = {key: value for key, value in tokens.items() if value}
                return self.tokens

        if tokens.get("connection_token") and not jwt_expired(tokens.get("connection_token")):
            self.tokens = tokens
            return tokens
        raise ExpressVPNAuthorizationRequired(
            "ExpressVPN needs authorization; open Settings → Proxy & VPN → VPN providers, "
            "select ExpressVPN and press l"
        )

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any] | None:
        response = self.request(
            "POST",
            f"{self.AUTH_BASE}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers=self.form_headers(),
            allow_error=True,
        )
        if not response.ok:
            return None
        data = _response_json(response, "ExpressVPN token refresh")
        return data if data.get("access_token") else None

    def get_srt(self) -> str | None:
        if self.srt and not jwt_expired(self.srt):
            return self.srt
        tokens = self.get_tokens()
        cached = _text(tokens.get("srt"))
        if cached and not jwt_expired(cached):
            self.srt = cached
            return cached
        access_token = _text(tokens.get("access_token"))
        if not access_token:
            return None
        response = self.request(
            "POST",
            f"{self.API_BASE}/srs2/subscription_receipts",
            headers=self.api_headers(access_token),
            json={},
        )
        data = _response_json(response, "ExpressVPN subscription receipts")
        receipts = data.get("srts") if isinstance(data.get("srts"), list) else []
        srt = self.select_srt(receipts, _text(tokens.get("subscription_id")))
        if not srt:
            raise ProxyResolutionError("ExpressVPN account has no active VPN subscription receipt")
        self.srt = tokens["srt"] = srt
        self.save_cache(tokens)
        return srt

    def get_connection_token(self) -> str | None:
        tokens = self.get_tokens()
        connection_token = _text(tokens.get("connection_token"))
        if connection_token and not jwt_expired(connection_token):
            return connection_token
        auth_token = self.get_srt() or _text(tokens.get("access_token"))
        if not auth_token:
            return None
        response = self.request(
            "POST",
            f"{self.API_BASE}/srs2/connection_token",
            headers=self.api_headers(auth_token),
            json={},
        )
        data = _response_json(response, "ExpressVPN connection token")
        connection_token = _text(data.get("cat") or data.get("connection_token") or data.get("token"))
        if connection_token:
            tokens["connection_token"] = connection_token
            self.save_cache(tokens)
        return connection_token or None

    @staticmethod
    def select_srt(receipts: list[dict[str, Any]], subscription_id: str) -> str | None:
        for receipt in receipts:
            srt = _text(receipt.get("srt"))
            if "xv.vpn" in (jwt_payload(srt).get("entitlements") or {}):
                return srt
        if subscription_id:
            for receipt in receipts:
                if _text(receipt.get("subscription_id")) == subscription_id:
                    return _text(receipt.get("srt")) or None
        return _text(receipts[0].get("srt")) or None if receipts else None

    def load_cache(self) -> dict[str, Any]:
        if self.cache_path.is_symlink() or not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(data) if isinstance(data, Mapping) else {}

    def save_cache(self, tokens: Mapping[str, Any]) -> None:
        payload = {str(key): value for key, value in tokens.items() if value}
        self.tokens = dict(payload)
        private_directory(self.cache_path.parent)
        with locked_path(self.cache_path):
            atomic_write_text(
                self.cache_path,
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )

    # --------------------------------------------------------------- locations
    def proxy(self, query: str) -> str | None:
        endpoint = self.resolve_endpoint(query.strip().lower())
        if not endpoint:
            return None
        connection_token = self.get_connection_token()
        if not connection_token:
            raise ProxyResolutionError("ExpressVPN did not return a connection token")
        return f"https://cat:{quote(connection_token, safe='')}@{endpoint}:443"

    def resolve_endpoint(self, query: str) -> str | None:
        self.last_name = self.last_host = None
        self.last_index = self.last_total = None
        query = self.server_map.get(query) or query
        if "expressprovider" in query:
            self.last_host = query if query.endswith(".expressprovider.com") else f"{query}.expressprovider.com"
            return self.last_host

        country, city, server_num = self.parse_query(query)
        if country and not city and (preset := self.region_map.get(country)):
            _, city, server_num = self.parse_query(f"{country}-{preset}")
        location = self.resolve_location(country, city) if country else self.resolve_slug(query)
        if not location:
            return None
        self.last_name = _text(location.get("name")) or None
        endpoints = self.get_endpoints(location)
        return self.pick_endpoint(endpoints, server_num) if endpoints else None

    def parse_query(self, query: str) -> tuple[str | None, str | None, int | None]:
        server_num = None
        match_number = re.match(r"^(.+?)[-]?(\d+)$", query)
        if match_number and re.search(r"[a-z]", match_number.group(1).rstrip("-")):
            value = int(match_number.group(2))
            if 1 <= value <= 99:
                query = match_number.group(1).rstrip("-")
                server_num = value
        match = re.match(r"^([a-z]{2})(?:-(.+))?$", query)
        if match:
            countries = {_text(item.get("country_code")).lower() for item in self.get_locations()}
            if match.group(1) in countries:
                return match.group(1), match.group(2) or None, server_num
        return None, None, None

    def resolve_location(self, country: str, city: str | None) -> dict[str, Any] | None:
        pool = [item for item in self.get_locations() if _text(item.get("country_code")).lower() == country]
        if not pool:
            return None
        if not city:
            return random.choice(pool)
        wanted = city.strip().lower()
        candidates: list[list[dict[str, Any]]] = [[], [], [], []]
        for location in pool:
            name = re.sub(r"^[A-Z]{2,}(?:\s*-\s*)", "", _text(location.get("name")), count=1).strip()
            slug = slugify(name)
            abbreviation = "".join(word[0] for word in re.findall(r"[a-zA-Z]+", name)).lower()
            if wanted == abbreviation:
                candidates[0].append(location)
            elif wanted == slug:
                candidates[1].append(location)
            elif slug.startswith(wanted):
                candidates[2].append(location)
            elif wanted in slug:
                candidates[3].append(location)
        return next((group[0] for group in candidates if group), None)

    def resolve_slug(self, query: str) -> dict[str, Any] | None:
        for location in self.get_locations():
            name = _text(location.get("name")).lower()
            keys = {
                _text(location.get("id")).lower(),
                name,
                _text(location.get("country_code")).lower(),
                slugify(name),
            }
            if query in keys:
                return location
        return None

    def pick_endpoint(self, endpoints: list[dict[str, Any]], server_num: int | None) -> str | None:
        hosts = [_text(item.get("host")).lower() for item in endpoints if item.get("host")]
        if not hosts:
            return None
        self.last_total = len(hosts)
        if server_num is None or not 1 <= server_num <= len(hosts):
            server_num = random.randint(1, len(hosts))
        self.last_index = server_num
        self.last_host = hosts[server_num - 1]
        return self.last_host

    def get_locations(self) -> list[dict[str, Any]]:
        if self.locations is None:
            srt = self.get_srt()
            if not srt:
                return []
            response = self.request(
                "GET",
                f"{self.API_BASE}/ids2/locations",
                headers=self.api_headers(srt),
                params={"protocols": "proxy"},
            )
            data = _response_json(response, "ExpressVPN locations")
            values = data.get("locations")
            self.locations = [dict(item) for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []
        return self.locations

    def get_endpoints(self, location: Mapping[str, Any]) -> list[dict[str, Any]]:
        srt = self.get_srt()
        if not srt:
            return []
        response = self.request(
            "POST",
            f"{self.API_BASE}/ids2/locations/{location.get('id')}/instances",
            headers=self.api_headers(srt),
            params={"protocols": "proxy"},
            json={},
        )
        data = _response_json(response, "ExpressVPN location instances")
        values = data.get("endpoints")
        return [dict(item) for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []

    # ---------------------------------------------------------------- transport
    def request(self, method: str, url: str, *, allow_error: bool = False, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise ProxyResolutionError(f"ExpressVPN request failed: {exc}") from exc
        if response.ok or allow_error:
            return response
        raise ProxyResolutionError(f"ExpressVPN request failed with HTTP {response.status_code}: {url}")

    def api_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
            "X-Client-App-Version": "12.69.0",
            "X-Client-OS": "Android",
            "X-Client-Device-Model": "SHIELD Android TV",
        }

    def form_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any, *, allow_empty: bool = False) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        _text(key).lower(): _text(item).lower()
        for key, item in value.items()
        if _text(key) and (allow_empty or _text(item))
    }


def _response_json(response: requests.Response, label: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ProxyResolutionError(f"{label} returned invalid JSON") from exc
    if not isinstance(data, Mapping):
        raise ProxyResolutionError(f"{label} returned a non-object response")
    return dict(data)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def jwt_payload(token: Any) -> dict[str, Any]:
    text = _text(token)
    if not text:
        return {}
    try:
        parts = text.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return dict(data) if isinstance(data, Mapping) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


def jwt_expired(token: Any) -> bool:
    if not _text(token):
        return True
    expires_at = jwt_payload(token).get("exp")
    if not expires_at:
        return False
    try:
        return time.time() >= int(expires_at) - 300
    except (TypeError, ValueError):
        return True

"""Resolve static proxies and VPN providers into one Requests-compatible URI.

The rest of UniDL deliberately sees only a URI.  Provider discovery belongs at
this boundary so API, manifest, licence and download traffic all use the same
resolved route.  Providers implemented here expose HTTPS proxy endpoints; none
of them changes the machine's default route or starts a system VPN silently.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests


class ProxyResolutionError(ValueError):
    """A configured proxy query could not be turned into a usable endpoint."""


PROVIDER_ALIASES = {
    "express": "expressvpn",
    "expressvpn": "expressvpn",
    "nord": "nordvpn",
    "nordvpn": "nordvpn",
    "surfshark": "surfsharkvpn",
    "surfsharkvpn": "surfsharkvpn",
    "windscribe": "windscribevpn",
    "windscribevpn": "windscribevpn",
    "basic": "basic",
}
PROVIDER_LABELS = {
    "expressvpn": "ExpressVPN",
    "nordvpn": "NordVPN",
    "surfsharkvpn": "Surfshark",
    "windscribevpn": "Windscribe",
    "basic": "Basic static proxies",
}
PROVIDER_ORDER = ("basic", "expressvpn", "nordvpn", "surfsharkvpn", "windscribevpn")

_URI_RE = re.compile(r"^(?:https?|socks(?:4|5)?h?)://", re.IGNORECASE)
_HOST_PORT_RE = re.compile(r"^(?:[^:@/\s]+(?::[^@/\s]+)?@)?[^:/\s]+:\d+$")
_REGION_RE = re.compile(r"^[a-z]{2}(?:\d+)?(?:(?::|-)[a-z0-9_-]+)?$", re.IGNORECASE)


def _enabled(entry: Any) -> bool:
    if not isinstance(entry, Mapping):
        return True
    # unshackle calls this key ``enable`` while UniDL's managers use the more
    # readable ``enabled``. Accept both so an imported provider block behaves
    # exactly like one created in the UI.
    value = entry.get("enabled", entry.get("enable", True))
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _urls(entry: Any) -> list[str]:
    """Normalise every supported legacy/new static proxy value."""
    if isinstance(entry, Mapping):
        if not _enabled(entry):
            return []
        entry = entry.get("urls", entry.get("url", entry.get("uri", entry.get("value", ""))))
    if isinstance(entry, str):
        values: Sequence[Any] = [entry]
    elif isinstance(entry, Sequence) and not isinstance(entry, (bytes, bytearray)):
        values = entry
    else:
        return []
    return [str(value).strip() for value in values if str(value or "").strip()]


def _normalise_uri(value: str) -> str:
    text = str(value or "").strip()
    if _URI_RE.match(text):
        parsed = urlsplit(text)
        if not parsed.hostname:
            raise ProxyResolutionError(f"Proxy URI has no hostname: {text!r}")
        return text
    if _HOST_PORT_RE.match(text):
        return f"http://{text}"
    return text


def _static_proxy(entries: Mapping[str, Any], query: str) -> str | None:
    """Resolve an exact name, or a 1-based suffix into a named list."""
    if query in entries:
        choices = _urls(entries[query])
        if not choices:
            raise ProxyResolutionError(f"Proxy {query!r} is disabled or has no endpoint")
        return _normalise_uri(random.choice(choices))

    indexed = re.fullmatch(r"(.+?)(\d+)", query)
    if indexed and indexed.group(1) in entries:
        choices = _urls(entries[indexed.group(1)])
        index = int(indexed.group(2)) - 1
        if index < 0 or index >= len(choices):
            raise ProxyResolutionError(
                f"Proxy {indexed.group(1)!r} has {len(choices)} endpoint(s), not {index + 1}"
            )
        return _normalise_uri(choices[index])
    return None


def _provider_specs(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        canonical = PROVIDER_ALIASES.get(str(name).strip().lower(), str(name).strip().lower())
        if isinstance(value, Mapping):
            found[canonical] = dict(value)
    return found


def _credentials(spec: Mapping[str, Any], provider: str) -> tuple[str, str]:
    username = str(spec.get("username") or "").strip()
    password = str(spec.get("password") or "").strip()
    if not username or not password:
        raise ProxyResolutionError(f"{PROVIDER_LABELS[provider]} needs service username and password")
    if provider in {"nordvpn", "surfsharkvpn"} and (
        "@" in username or not re.fullmatch(r"[a-z0-9]{48}", username + password, re.IGNORECASE)
    ):
        raise ProxyResolutionError(
            f"{PROVIDER_LABELS[provider]} needs its 48-character service credentials, not account login details"
        )
    return quote(username, safe=""), quote(password, safe="")


def _server_map(spec: Mapping[str, Any]) -> dict[str, Any]:
    value = spec.get("server_map") or {}
    return {str(key).strip().lower(): item for key, item in value.items()} if isinstance(value, Mapping) else {}


_PROVIDER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _get_json(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    try:
        response = requests.get(url, params=params, headers=headers or _PROVIDER_HEADERS, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise ProxyResolutionError(f"Proxy provider request failed: {exc}") from exc
    except ValueError as exc:
        raise ProxyResolutionError(f"Proxy provider returned invalid JSON: {url}") from exc


def _split_query(query: str) -> tuple[str, str]:
    region, separator, city = query.lower().partition(":")
    return region, city if separator else ""


def _nordvpn(spec: Mapping[str, Any], query: str) -> str | None:
    username, password = _credentials(spec, "nordvpn")
    region, city = _split_query(query)
    if re.fullmatch(r"[a-z]{2}\d+", region):
        hostname = f"{region}.proxy.nordvpn.com"
    else:
        countries = _get_json("https://api.nordvpn.com/v1/servers/countries")
        country = next(
            (
                item
                for item in countries
                if (region.isdigit() and int(item.get("id", -1)) == int(region))
                or str(item.get("code", "")).lower() == region
            ),
            None,
        )
        if not country:
            return None
        mapped = _server_map(spec).get(f"{region}:{city}" if city else region)
        if mapped:
            hostname = f"{str(country.get('code', region)).lower()}{mapped}.proxy.nordvpn.com"
        else:
            servers = _get_json(
                "https://api.nordvpn.com/v1/servers/recommendations",
                params={"filters[country_id]": country["id"]},
            )
            if city:
                wanted = city.casefold()
                servers = [
                    server
                    for server in servers
                    if any(
                        str(
                            location.get("city", {}).get("name", "")
                            if isinstance(location.get("city"), Mapping)
                            else location.get("city", "")
                        ).casefold()
                        == wanted
                        for location in server.get("locations", [])
                    )
                ]
            if not servers:
                detail = f" in {city}" if city else ""
                raise ProxyResolutionError(f"NordVPN has no recommended {region.upper()} server{detail}")
            hostname = str(random.choice(servers).get("hostname") or "")
            if hostname.endswith(".nordvpn.com") and not hostname.endswith(".proxy.nordvpn.com"):
                hostname = hostname[: -len(".nordvpn.com")] + ".proxy.nordvpn.com"
    return f"https://{username}:{password}@{hostname}:89"


def _surfshark(spec: Mapping[str, Any], query: str) -> str | None:
    username, password = _credentials(spec, "surfsharkvpn")
    region, city = _split_query(query)
    clusters = _get_json(
        "https://api.surfshark.com/v3/server/clusters/all",
        headers=_PROVIDER_HEADERS,
    )
    mapped = _server_map(spec).get(f"{region}:{city}" if city else region)
    if mapped:
        token = str(mapped)
        hostname = token if "." in token else f"{region}{token}.prod.surfshark.com"
    elif re.fullmatch(r"[a-z]{2}-[a-z0-9-]+", region):
        hostname = f"{region}.prod.surfshark.com"
    else:
        matches = [
            item
            for item in clusters
            if str(item.get("countryCode", "")).lower() == region
            or (region.isdigit() and str(item.get("id", "")) == region)
        ]
        if city:
            matches = [
                item
                for item in matches
                if city.casefold()
                in {str(item.get("city", "")).casefold(), str(item.get("location", "")).casefold()}
            ]
        hostnames = [str(item.get("connectionName") or "") for item in matches if item.get("connectionName")]
        if not hostnames:
            return None
        hostname = random.choice(hostnames)
    return f"https://{username}:{password}@{hostname}:443"


def _windscribe(spec: Mapping[str, Any], query: str) -> str | None:
    username, password = _credentials(spec, "windscribevpn")
    region, city = _split_query(query)
    mapped = _server_map(spec).get(f"{region}:{city}" if city else region)
    if mapped:
        hostname = str(mapped).split(":", 1)[0]
    else:
        payload = _get_json("https://assets.windscribe.com/serverlist/firefox/1/1", headers=_PROVIDER_HEADERS)
        locations = payload.get("data", []) if isinstance(payload, Mapping) else []
        wanted_number = ""
        numbered = re.fullmatch(r"([a-z]{2})(\d+)", region)
        if numbered:
            region, wanted_number = numbered.groups()
        hostnames: list[str] = []
        for location in locations:
            if str(location.get("country_code", "")).lower() != region:
                continue
            for group in location.get("groups", []):
                if city and str(group.get("city", "")).casefold() != city.casefold():
                    continue
                for host in group.get("hosts", []):
                    hostname = str(host.get("hostname") or "")
                    if not hostname:
                        continue
                    if wanted_number:
                        number = wanted_number.lstrip("0") or "0"
                        prefixes = (f"{region}-{wanted_number}.", f"{region}-{number}.", f"{region}-{number.zfill(3)}.")
                        if not hostname.startswith(prefixes):
                            continue
                    hostnames.append(hostname.split(":", 1)[0])
        if not hostnames:
            return None
        hostname = random.choice(hostnames)
    return f"https://{username}:{password}@{hostname}:443"


def _provider_proxy(
    name: str,
    spec: Mapping[str, Any],
    query: str,
    token_root: Path | None = None,
) -> str | None:
    if not _enabled(spec):
        raise ProxyResolutionError(f"{PROVIDER_LABELS.get(name, name)} is disabled")
    if name == "basic":
        regions = spec.get("regions") if isinstance(spec.get("regions"), Mapping) else spec
        ignored = {"enabled", "regions"}
        entries = {str(key): value for key, value in regions.items() if key not in ignored}
        return _static_proxy(entries, query)
    if name == "expressvpn":
        from .proxy_express import ExpressVPNClient

        root = Path(token_root) if token_root is not None else Path.home() / ".unidl" / "tokens" / "vpn"
        return ExpressVPNClient(spec, root).proxy(query)
    if name == "nordvpn":
        return _nordvpn(spec, query)
    if name == "surfsharkvpn":
        return _surfshark(spec, query)
    if name == "windscribevpn":
        return _windscribe(spec, query)
    raise ProxyResolutionError(f"Unsupported proxy provider: {name}")


def resolve_proxy(
    selection: str | None,
    proxies: Any,
    providers: Any,
    token_root: Path | None = None,
) -> str | None:
    """Resolve a saved name, direct URI or ``provider:region`` query.

    Unknown legacy values are returned unchanged.  That preserves configurations
    which passed an unusual Requests-compatible proxy spelling before provider
    support existed, while recognized provider queries fail clearly.
    """
    query = str(selection or "").strip()
    if not query:
        return None
    if _URI_RE.match(query) or _HOST_PORT_RE.match(query):
        return _normalise_uri(query)

    static = proxies if isinstance(proxies, Mapping) else {}
    matched = _static_proxy(static, query)
    if matched:
        # A named entry may itself contain a provider query.
        if matched != query and not (_URI_RE.match(matched) or _HOST_PORT_RE.match(matched)):
            return resolve_proxy(matched, {}, providers, token_root)
        return matched

    specs = _provider_specs(providers)
    express_cache = (
        Path(token_root) / "expressvpn_tokens.json"
        if token_root is not None
        else None
    )
    if "expressvpn" not in specs and express_cache is not None and express_cache.is_file():
        # Match unshackle's useful auto-load behavior after the one-time login.
        # The cached session is still inert until the user chooses an ExpressVPN
        # route, so discovering it does not turn a proxy on by itself.
        specs["expressvpn"] = {}
    prefix, separator, remainder = query.partition(":")
    canonical = PROVIDER_ALIASES.get(prefix.lower())
    if separator and canonical:
        spec = specs.get(canonical)
        if spec is None:
            raise ProxyResolutionError(f"{PROVIDER_LABELS[canonical]} is not configured")
        resolved = _provider_proxy(canonical, spec, remainder, token_root)
        if not resolved:
            raise ProxyResolutionError(f"{PROVIDER_LABELS[canonical]} has no proxy for {remainder!r}")
        return resolved

    if _REGION_RE.fullmatch(query) and specs:
        failures: list[str] = []
        for name in PROVIDER_ORDER:
            spec = specs.get(name)
            if spec is None or not _enabled(spec):
                continue
            try:
                if resolved := _provider_proxy(name, spec, query, token_root):
                    return resolved
            except ProxyResolutionError as exc:
                failures.append(str(exc))
        detail = f": {'; '.join(failures)}" if failures else ""
        raise ProxyResolutionError(f"No enabled proxy provider has a route for {query!r}{detail}")

    return _normalise_uri(query)


def safe_proxy_label(uri: str | None) -> str:
    """Describe a resolved proxy without exposing embedded credentials."""
    if not uri:
        return "direct connection"
    parsed = urlsplit(uri if _URI_RE.match(uri) else f"http://{uri}")
    if parsed.hostname:
        try:
            parsed_port = parsed.port
        except ValueError:
            parsed_port = None
        port = f":{parsed_port}" if parsed_port else ""
        return f"{parsed.scheme or 'http'}://{parsed.hostname}{port}"
    return "configured proxy"

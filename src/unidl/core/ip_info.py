"""Consolidated public IP geolocation and timezone lookup (with proxy support)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True, slots=True)
class IpInfo:
    """Normalized exit IP geolocation and timezone metadata."""

    ip: str
    country: str = ""
    region: str = ""
    city: str = ""
    timezone: str = ""
    org: str = ""
    asn: str = ""
    source: str = ""

    def has_fields(self, fields: Sequence[str]) -> bool:
        for name in fields:
            if not getattr(self, str(name), ""):
                return False
        return True

    def summary(self) -> str:
        loc = ", ".join(part for part in (self.city, self.region, self.country) if part)
        bits = [self.ip]
        if loc:
            bits.append(loc)
        if self.timezone:
            bits.append(self.timezone)
        if self.org:
            bits.append(self.org)
        return " · ".join(bits)


def _fetch_ipinfo(
    *,
    proxies: dict[str, str] | None,
    token: str,
    timeout: float,
    session: Any,
) -> IpInfo | None:
    headers = {"Accept": "application/json", "User-Agent": "UniDL/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    getter = session.get if session is not None else requests.get
    try:
        resp = getter("https://ipinfo.io/json", headers=headers, proxies=proxies, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, Mapping) or not data.get("ip"):
        return None
    org_raw = str(data.get("org") or "").strip()
    asn = ""
    org = org_raw
    if org_raw.upper().startswith("AS") and " " in org_raw:
        asn, org = org_raw.split(" ", 1)
    return IpInfo(
        ip=str(data.get("ip") or "").strip(),
        country=str(data.get("country") or "").strip().upper(),
        region=str(data.get("region") or "").strip(),
        city=str(data.get("city") or "").strip(),
        timezone=str(data.get("timezone") or "").strip(),
        org=org.strip(),
        asn=asn.strip(),
        source="ipinfo.io",
    )


def _fetch_ip_api(
    *,
    proxies: dict[str, str] | None,
    timeout: float,
    session: Any,
) -> IpInfo | None:
    getter = session.get if session is not None else requests.get
    url = "http://ip-api.com/json/?fields=status,query,countryCode,regionName,city,timezone,isp,as"
    try:
        resp = getter(url, headers={"Accept": "application/json"}, proxies=proxies, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, Mapping) or data.get("status") != "success" or not data.get("query"):
        return None
    as_raw = str(data.get("as") or "").strip()
    asn = as_raw.split(" ", 1)[0] if as_raw.upper().startswith("AS") else ""
    return IpInfo(
        ip=str(data.get("query") or "").strip(),
        country=str(data.get("countryCode") or "").strip().upper(),
        region=str(data.get("regionName") or "").strip(),
        city=str(data.get("city") or "").strip(),
        timezone=str(data.get("timezone") or "").strip(),
        org=str(data.get("isp") or "").strip(),
        asn=asn,
        source="ip-api.com",
    )


def _merge(primary: IpInfo, fallback: IpInfo) -> IpInfo:
    return IpInfo(
        ip=primary.ip or fallback.ip,
        country=primary.country or fallback.country,
        region=primary.region or fallback.region,
        city=primary.city or fallback.city,
        timezone=primary.timezone or fallback.timezone,
        org=primary.org or fallback.org,
        asn=primary.asn or fallback.asn,
        source=f"{primary.source}+{fallback.source}" if primary.source and fallback.source else (primary.source or fallback.source),
    )


def lookup_ip_info(
    *,
    proxy: str | None = None,
    token: str = "",
    timeout: float = 10.0,
    required_fields: Sequence[str] = (),
    session: Any = None,
) -> IpInfo:
    """Resolve current exit IP, country, region, city and IANA timezone.

    Uses ``ipinfo.io`` first (with optional API token from ``token`` or
    ``UNIDL_IPINFO_TOKEN``) and transparently falls back to ``ip-api.com`` if
    unavailable or missing any field in ``required_fields``.
    """
    resolved_token = str(token or os.environ.get("UNIDL_IPINFO_TOKEN", "")).strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    primary = _fetch_ipinfo(proxies=proxies, token=resolved_token, timeout=timeout, session=session)
    if primary is not None and primary.has_fields(required_fields):
        return primary
    fallback = _fetch_ip_api(proxies=proxies, timeout=timeout, session=session)
    if primary is not None and fallback is not None:
        return _merge(primary, fallback)
    if primary is not None:
        return primary
    if fallback is not None:
        return fallback
    raise RuntimeError("Could not determine public IP information from ipinfo.io or ip-api.com.")


__all__ = ["IpInfo", "lookup_ip_info"]

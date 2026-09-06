"""Widevine: the exchange, and nothing else.

The 137 scripts that do DRM each carry a copy of the same eight lines:
``Device.load`` -> ``Cdm.from_device`` -> ``open`` -> ``get_license_challenge``
-> POST -> ``parse_license`` -> ``get_keys`` -> filter ``SIGNING``. The only
part that genuinely differs is the POST, so that is the only part a service
implements (``Service.get_license``).

What is *not* here any more: which DRM systems exist, and how a device file is
recognised. Those became facts about a set of systems rather than about Widevine
when a third one arrived, and they live in :mod:`unidl.core.drm`. The names are
re-exported below because plenty of call sites and checks import them from here,
and moving a file should not be a rename.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .drm import (
    MONALISA,
    PLAYREADY,
    WIDEVINE,
    CdmError,
    DeviceFile,
    DeviceInfo,
    discover,
    suffixes,
    system_of,
)


def load_device(path: Path) -> Any:
    if not path.is_file():
        raise CdmError(f"Widevine device file not found: {path}")
    try:
        # pywidevine is optional and expensive to import.  Loading a device is
        # already the first point at which it is needed, so keep it out of the
        # TUI/home-screen import path.
        from pywidevine.device import Device

        return Device.load(str(path))
    except Exception as exc:  # pywidevine raises assorted types
        raise CdmError(f"Could not load {path.name}: {exc}") from exc


def device_summary(path: Path) -> str:
    """Short description of a wvd, for the settings screen."""
    try:
        device = load_device(path)
    except CdmError as exc:
        return f"unusable ({exc})"
    level = getattr(device, "security_level", "?")
    system_id = getattr(device, "system_id", "?")
    return f"L{level} system_id={system_id}"


LicenseTransport = Callable[[bytes], bytes]
"""Takes a challenge, returns the raw license response body."""


__all__ = [
    "MONALISA",
    "PLAYREADY",
    "WIDEVINE",
    "CdmError",
    "DeviceFile",
    "DeviceInfo",
    "LicenseTransport",
    "device_summary",
    "discover",
    "get_keys",
    "load_device",
    "suffixes",
    "system_of",
]


def get_keys(
    device_path: Path,
    pssh: str,
    transport: LicenseTransport,
    *,
    service_certificate: bytes | None = None,
) -> list[str]:
    """Run one full Widevine exchange and return ``kid:key`` strings."""
    # Keep pywidevine out of application startup.  This exchange is the only
    # place in this module that needs the CDM and PSSH implementations.
    from pywidevine.cdm import Cdm
    from pywidevine.pssh import PSSH

    device = load_device(device_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    try:
        if service_certificate:
            cdm.set_service_certificate(session_id, service_certificate)
        try:
            pssh_obj = PSSH(pssh)
        except Exception as exc:
            raise CdmError(f"Invalid PSSH: {exc}") from exc

        challenge = cdm.get_license_challenge(session_id, pssh_obj)
        response = transport(challenge)
        if not response:
            raise CdmError("License server returned an empty response")
        cdm.parse_license(session_id, response)

        keys = [
            f"{key.kid.hex}:{key.key.hex()}"
            for key in cdm.get_keys(session_id)
            if key.type != "SIGNING"
        ]
        if not keys:
            raise CdmError("License parsed but contained no content keys")
        return keys
    finally:
        try:
            cdm.close(session_id)
        except Exception:
            pass

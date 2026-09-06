"""MonaLisa support, the DRM iQiyi uses.

Shaped like :mod:`unidl.core.cdm` and :mod:`unidl.core.playready` on purpose, so
the registry can treat all three the same way. One thing genuinely differs, and
it is the interesting part: there is no licence request.

Widevine and PlayReady both build a challenge, post it somewhere, and parse what
comes back. MonaLisa's ticket *is* the licence - iQiyi returns it inside the
playback response, next to the stream URLs - and the device is a WebAssembly
module that unwraps it locally. So this module takes a ticket and returns keys,
with no transport argument and nothing to fail over the network.

The device file is a ``.mld``: a small JSON pointing at the wasm module beside
it. ``Module.load`` resolves that path relative to the ``.mld``, which means the
two files travel together and a device copied without its module fails at load
with something unhelpful - hence the check for it here.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

INSTALL_HINT = (
    "MonaLisa needs pymonalisa: python -m pip install pymonalisa\n"
    "    then configure a .mld device and its wasm module under paths.cdm in unidl.yaml"
)


class MonaLisaUnavailable(RuntimeError):
    """pymonalisa is not installed, or the device is unusable."""


def available() -> bool:
    return importlib.util.find_spec("pymonalisa") is not None


def require() -> None:
    if not available():
        raise MonaLisaUnavailable(f"MonaLisa is not available.\n    {INSTALL_HINT}")


def is_device(path: Path) -> bool:
    return Path(path).suffix.lower() == ".mld"


def module_path(device_path: Path | str) -> Path | None:
    """Where the ``.mld`` says its wasm module is, resolved for real.

    Read here as well as by pymonalisa so a missing module can be reported as
    "the module is missing" rather than as a wasm parse failure.
    """
    path = Path(device_path)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    declared = data.get("wasm_path")
    if not declared:
        return None
    candidate = Path(declared)
    return candidate if candidate.is_absolute() else path.parent / candidate


def device_summary(device_path: Path | str) -> str:
    """Short description for the picker, without loading the wasm module.

    Loading it costs a wasmtime compile of a several-megabyte module, which is
    not something a list of devices should do per row.
    """
    path = Path(device_path)
    if not path.is_file():
        return "missing"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    meta = data.get("metadata") or {}
    version = str(meta.get("version") or "").strip()
    name = str(meta.get("name") or "").strip()
    module = module_path(path)
    if module is None or not module.is_file():
        return f"{name or 'MonaLisa'} - wasm module missing"
    return " ".join(part for part in (name, version) if part) or "MonaLisa device"


def get_keys(device_path: Path | str, ticket: str) -> list[str]:
    """Unwrap a licence ticket into ``kid:key`` strings.

    No transport argument, unlike the other two systems: everything happens in
    the module. A session is opened and closed per ticket, the same as elsewhere,
    because the module keeps state per session and reusing one across titles is
    how you get the previous title's keys back.
    """
    require()
    path = Path(device_path)
    if not path.is_file():
        raise MonaLisaUnavailable(f"MonaLisa device file not found: {path}")
    module_file = module_path(path)
    if module_file is None:
        raise MonaLisaUnavailable(f"{path.name} does not say where its wasm module is")
    if not module_file.is_file():
        raise MonaLisaUnavailable(
            f"{path.name} points at {module_file.name}, which is not there. "
            "The device and its module have to sit together."
        )
    if not (ticket or "").strip():
        raise MonaLisaUnavailable("No licence ticket to unwrap")

    from pymonalisa.cdm import Cdm
    from pymonalisa.license import License
    from pymonalisa.module import Module
    from pymonalisa.types import KeyType

    try:
        module = Module.load(str(path))
    except Exception as exc:
        raise MonaLisaUnavailable(f"Could not load {path.name}: {exc}") from exc

    cdm = Cdm.from_module(module)
    session_id = cdm.open()
    try:
        cdm.parse_license(session_id, License(ticket))
        keys = cdm.get_keys(session_id, KeyType.CONTENT)
        pairs = [f"{_hex(key.kid)}:{_hex(key.key)}" for key in keys or []]
        pairs = [pair for pair in pairs if ":" in pair and pair.split(":")[0]]
        if not pairs:
            raise MonaLisaUnavailable("The ticket parsed but held no content keys")
        return pairs
    except MonaLisaUnavailable:
        raise
    except Exception as exc:
        raise MonaLisaUnavailable(f"Could not unwrap the ticket: {exc}") from exc
    finally:
        try:
            cdm.close(session_id)
        except Exception:
            pass


def _hex(value) -> str:
    """``kid`` is bytes here, where pywidevine hands back a UUID. Take either."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return getattr(value, "hex", lambda: str(value))() if callable(
        getattr(value, "hex", None)
    ) else str(getattr(value, "hex", value))


__all__ = [
    "INSTALL_HINT",
    "MonaLisaUnavailable",
    "available",
    "device_summary",
    "get_keys",
    "is_device",
    "module_path",
    "require",
]

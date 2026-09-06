"""A CDM that lives on somebody else's machine.

The device file is the part of this project that cannot be shared, so a lot of
setups keep it on a server and expose the three steps over HTTP instead. The
protocol is pywidevine's ``serve`` API, which pyplayready's server copies, and
which the working Netflix script talks to already:

===============================================  ==========================
``GET  {host}/{device}/open``                     a session id, and the device
``POST {host}/{device}/set_service_certificate``  optional, Widevine only
``POST {host}/{device}/get_license_challenge``    init data in, challenge out
``POST {host}/{device}/parse_license``            the licence response in
``POST {host}/{device}/get_keys``                 the content keys out
``GET  {host}/{device}/close/{session}``          tidy up
===============================================  ==========================

Authenticated with an ``X-Secret-Key`` header. Every response is
``{"status"|"message", "data": {...}}`` and a request that worked says so in one
of those two fields - which is why :meth:`_ok` looks at both rather than trusting
the HTTP status.

Three details are not guesses, they are what a real server needs:

* **The challenge path differs by system.** PlayReady servers answer
  ``get_license_challenge``; Widevine servers answer
  ``get_license_challenge/STREAMING``. Both are tried, in the order that is right
  for the system, because a server answering "not found" for the other one is
  normal rather than an error.
* **The licence body differs by system.** PlayReady exchanges XML as text;
  Widevine exchanges bytes as base64. Sending base64 to a PlayReady server gets a
  parse error from deep inside its CDM.
* **The device is verified on open.** The server states its own ``system_id`` and
  ``security_level``, and a mismatch with the config means the name resolved to a
  different device than intended - worth failing on, because the alternative is a
  licence request that is refused for a reason that looks like the service's.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

import requests

from .drm import PLAYREADY, WIDEVINE, CdmError

_HEX = re.compile(r"[^0-9a-fA-F]")


@dataclass(frozen=True)
class RemoteCdmConfig:
    """One entry from the ``remote_cdm`` section of ``unidl.yaml``."""

    name: str
    system: str = WIDEVINE
    device_name: str = ""
    device_type: str = ""
    system_id: int | None = None
    security_level: int | None = None
    host: str = ""
    secret: str = ""
    timeout: float = 30.0
    #: Disabled entries remain visible in the manager but never enter a picker
    #: or a licence exchange. This is separate from the app-wide CDM choice: it
    #: lets a server stay configured without being an available destination.
    enabled: bool = True

    @property
    def usable(self) -> bool:
        return bool(self.host and self.device_name)

    @property
    def level(self) -> str:
        """The security level, in the form the device pickers already show."""
        if self.security_level is None:
            return ""
        return (
            f"SL{self.security_level}"
            if self.system == PLAYREADY or self.security_level >= 1000
            else f"L{self.security_level}"
        )

    def line(self) -> str:
        bits = [self.device_name or "?", self.level or "?", self.host or "?"]
        return "  ".join(bit for bit in bits if bit)


def parse_entry(entry: dict[str, Any]) -> RemoteCdmConfig | None:
    """Read one config entry.

    Keys are normalised - lowercased, spaces to underscores - because the two
    documented spellings disagree: unshackle's example file uses ``device_type``
    while its loader reads ``Device Type``. Accepting both costs one line and
    means a config copied from either place works.
    """
    if not isinstance(entry, dict):
        return None
    data = {
        str(key).strip().lower().replace(" ", "_").replace("-", "_"): value
        for key, value in entry.items()
    }
    name = str(data.get("name") or "").strip()
    device_name = str(data.get("device_name") or data.get("device") or "").strip()
    if not name and not device_name:
        return None
    device_type = str(data.get("device_type") or "").strip()
    security_level = _int(data.get("security_level"))

    system = str(data.get("system") or "").strip().lower()
    if not system:
        # Inferred, because the existing config format has no system field: a
        # PlayReady device says so in its type, and its levels are four digits.
        if device_type.upper() == "PLAYREADY" or (security_level or 0) >= 1000:
            system = PLAYREADY
        else:
            system = WIDEVINE

    return RemoteCdmConfig(
        name=name or device_name,
        system=system,
        device_name=device_name or name,
        device_type=device_type,
        system_id=_int(data.get("system_id")),
        security_level=security_level,
        host=str(data.get("host") or "").strip(),
        secret=str(data.get("secret") or data.get("token") or data.get("key") or "").strip(),
        timeout=float(data.get("timeout") or 30.0),
        enabled=_bool(data.get("enabled"), True),
    )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


class RemoteCdm:
    """One session against a remote CDM.

    Used as a context manager so the session is closed even when the licence
    request fails: a server that hands out a limited number of concurrent
    sessions will otherwise refuse the next run for a reason that has nothing to
    do with it.
    """

    def __init__(self, config: RemoteCdmConfig, log=None):
        if not config.usable:
            raise CdmError(
                f"remote CDM {config.name!r} needs both a host and a device_name in unidl.yaml"
            )
        self.config = config
        self.log = log or (lambda _message: None)
        self.base = f"{config.host.rstrip('/')}/{config.device_name}"
        self.session_id: str = ""
        self.http = requests.Session()
        self.http.headers.update(
            {"Content-Type": "application/json", "X-Secret-Key": config.secret}
        )

    # ----------------------------------------------------------- lifecycle
    def __enter__(self) -> RemoteCdm:
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def open(self) -> str:
        if self.session_id:
            return self.session_id
        data = self._data(self._get("open"))
        session_id = str(data.get("session_id") or "")
        if not session_id:
            raise CdmError(f"remote CDM {self.config.name}: open returned no session id")
        # Keep it before validating the advertised device. A mismatch is still an
        # opened server session and must be closed rather than leaked.
        self.session_id = session_id
        device = data.get("device") or {}
        try:
            self._verify("system_id", self.config.system_id, device.get("system_id"))
            self._verify(
                "security_level", self.config.security_level, device.get("security_level")
            )
        except Exception:
            self.close()
            raise
        self.log(
            f"remote CDM {self.config.name}: session open "
            f"({device.get('device_name') or self.config.device_name})"
        )
        return session_id

    def close(self) -> None:
        if not self.session_id:
            return
        session_id, self.session_id = self.session_id, ""
        for attempt in (
            lambda: self._get(f"close/{session_id}"),
            lambda: self._post("close", {"session_id": session_id}),
        ):
            try:
                attempt()
                return
            except (CdmError, requests.RequestException):
                continue
        # Not raised: the keys are already in hand by this point, and a server
        # that will not close a session is its own problem, not this download's.
        self.log(f"remote CDM {self.config.name}: session was not closed cleanly")

    def _verify(self, field: str, expected: int | None, reported: Any) -> None:
        if expected is None or reported is None:
            return
        if int(reported) != int(expected):
            raise CdmError(
                f"remote CDM {self.config.name}: {field} is {reported} on the server "
                f"but {expected} in unidl.yaml - the name is pointing at a "
                "different device than you think"
            )

    # ------------------------------------------------------------ exchange
    def set_service_certificate(self, certificate: bytes | str | None) -> None:
        """Widevine privacy mode. PlayReady servers answer "not found"; that is fine."""
        if not certificate:
            return
        try:
            self._post(
                "set_service_certificate",
                {"session_id": self.open(), "certificate": _b64(certificate)},
            )
        except CdmError as exc:
            if "not found" in str(exc).lower():
                return
            raise

    def challenge(self, init_data: str, *, privacy_mode: bool = False) -> bytes:
        body = {
            "session_id": self.open(),
            "init_data": init_data,
            "privacy_mode": privacy_mode,
        }
        paths = (
            ("get_license_challenge", "get_license_challenge/STREAMING")
            if self.config.system == PLAYREADY
            else ("get_license_challenge/STREAMING", "get_license_challenge")
        )
        data = self._first(paths, body, "challenge")
        raw = data.get("challenge_b64") or data.get("challenge")
        if not raw:
            raise CdmError(f"remote CDM {self.config.name}: no challenge in the answer")
        if data.get("challenge_b64"):
            return base64.b64decode(raw)
        return raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)

    def parse_license(self, response: bytes | str) -> None:
        self._post(
            "parse_license",
            {"session_id": self.open(), "license_message": self._license_body(response)},
        )

    def keys(self) -> list[str]:
        data = self._first(("get_keys", "get_keys/ALL"), {"session_id": self.open()}, "keys")
        found: list[str] = []
        for entry in data.get("keys") or []:
            if str(entry.get("type") or entry.get("key_type") or "").upper() == "SIGNING":
                continue
            kid = _HEX.sub("", str(entry.get("kid") or entry.get("key_id") or "")).lower()
            key = _HEX.sub("", str(entry.get("key") or "")).lower()
            if len(kid) == 32 and len(key) == 32:
                found.append(f"{kid}:{key}")
        if not found:
            raise CdmError(
                f"remote CDM {self.config.name}: the licence was accepted but "
                "no content keys came back"
            )
        return found

    def _license_body(self, response: bytes | str) -> str:
        """PlayReady wants the SOAP text; Widevine wants base64 bytes."""
        if self.config.system != PLAYREADY:
            return _b64(response)
        if isinstance(response, bytes):
            try:
                return response.decode("utf-8")
            except UnicodeDecodeError:
                return base64.b64encode(response).decode("ascii")
        text = str(response)
        if text.lstrip().startswith("<"):
            return text
        # A server that handed back base64 of the XML: unwrap it, because the
        # other end is going to parse it as XML either way.
        try:
            decoded = base64.b64decode(text, validate=True).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return text
        return decoded if decoded.lstrip().startswith("<") else text

    # ------------------------------------------------------------ plumbing
    def _get(self, path: str) -> dict[str, Any]:
        return self._request("get", path)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("post", path, json=body)

    def _first(
        self, paths: tuple[str, ...], body: dict[str, Any], what: str
    ) -> dict[str, Any]:
        """Try each path, treating "not found" as "this server spells it differently"."""
        last: Exception | None = None
        for path in paths:
            try:
                return self._data(self._post(path, body))
            except CdmError as exc:
                last = exc
                if "not found" in str(exc).lower():
                    continue
                raise
        raise CdmError(f"remote CDM {self.config.name}: no endpoint answered for {what} ({last})")

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base}/{path}"
        try:
            response = self.http.request(method, url, timeout=self.config.timeout, **kwargs)
        except requests.RequestException as exc:
            raise CdmError(f"remote CDM {self.config.name}: could not reach {url} ({exc})") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            preview = (response.text or "")[:120]
            raise CdmError(
                f"remote CDM {self.config.name}: {path} answered "
                f"{response.status_code} with something that is not JSON ({preview})"
            ) from exc
        if not isinstance(payload, dict):
            raise CdmError(f"remote CDM {self.config.name}: {path} answered with a {type(payload).__name__}")
        if response.status_code >= 400 or not self._ok(payload):
            message = str(
                payload.get("message") or payload.get("Error") or payload.get("error") or ""
            ).strip()
            raise CdmError(
                f"remote CDM {self.config.name}: {path} refused "
                f"({response.status_code}{f' - {message}' if message else ''})"
            )
        return payload

    @staticmethod
    def _ok(payload: dict[str, Any]) -> bool:
        status = payload.get("status")
        if status is not None:
            try:
                return int(status) == 200
            except (TypeError, ValueError):
                return str(status).strip().lower() in {"ok", "success"}
        message = str(payload.get("message") or "").strip().lower()
        return message in {"success", "ok"} or message.startswith("successfully")

    @staticmethod
    def _data(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}


def _b64(value: bytes | str) -> str:
    if isinstance(value, str):
        try:
            base64.b64decode(value, validate=True)
            return value  # already base64, do not double-encode
        except Exception:  # noqa: BLE001
            value = value.encode("utf-8")
    return base64.b64encode(value).decode("ascii")


# ------------------------------------------------------------- key getters


def widevine_keys(
    config: RemoteCdmConfig,
    init_data: str,
    transport,
    *,
    certificate: bytes | None = None,
    log=None,
) -> list[str]:
    """One Widevine exchange, with the CDM at the other end of a socket."""
    with RemoteCdm(config, log=log) as cdm:
        cdm.set_service_certificate(certificate)
        challenge = cdm.challenge(init_data, privacy_mode=bool(certificate))
        response = transport(challenge)
        if not response:
            raise CdmError("the licence server returned an empty response")
        cdm.parse_license(response)
        return cdm.keys()


def playready_keys(
    config: RemoteCdmConfig, init_data: str, transport, *, log=None
) -> list[str]:
    """One PlayReady exchange. The challenge is XML, so it is passed as text."""
    with RemoteCdm(config, log=log) as cdm:
        challenge = cdm.challenge(init_data)
        response = transport(challenge.decode("utf-8", "ignore"))
        if not response:
            raise CdmError("the licence server returned an empty response")
        cdm.parse_license(response)
        return cdm.keys()


__all__ = [
    "RemoteCdm",
    "RemoteCdmConfig",
    "parse_entry",
    "playready_keys",
    "widevine_keys",
]

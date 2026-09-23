"""Debug-mode diagnostics and safe HTTP request tracing.

The recorder is intentionally opt-in and context-local.  Services keep using
their normal ``requests`` sessions; when a debug session is active the small
request hook below records API metadata without making the service depend on a
logging framework.  Credentials are redacted, while signed media manifests are
kept verbatim because their exact URL is often required to reproduce a failure.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import re
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

try:  # Optional service client; only hook it when the dependency is installed.
    import httpx as _httpx
except Exception:  # pragma: no cover - minimal installations do not ship httpx
    _httpx = None

_CURRENT: contextvars.ContextVar[DebugRecorder | None] = contextvars.ContextVar(
    "unidl_debug_recorder", default=None
)

_SECRET_NAME = re.compile(
    r"(?:^|[-_])(authorization|cookie|set-cookie|password|passwd|token|access[-_]?token|refresh[-_]?token|"
    r"id[-_]?token|client[-_]?secret|secret|api[-_]?key|signature|sig|hash|session[-_]?id|sessionid|"
    r"jsessionid|code)(?:$|[-_])",
    re.I,
)
_SECRET_QUERY = re.compile(
    r"^(?:token|access[_-]?token|refresh[_-]?token|id[_-]?token|authorization|auth|signature|sig|hash|"
    r"session(?:id)?|jsessionid|password|passwd|code|key)$",
    re.I,
)
_MANIFEST_PATH = re.compile(
    r"(?:\.mpd|\.m3u8?|m3u8|/manifest(?:/|$)|/playlist(?:/|$)|/master(?:/|$))",
    re.I,
)
_MEDIA_PATH = re.compile(
    r"(?:\.m4s|\.mp4|\.m4a|\.ts|\.aac|\.ac3|\.eac3|/segment(?:/|$)|/chunk(?:/|$))",
    re.I,
)
_MEDIA_CONTENT = re.compile(
    r"(?:^|/)(?:video|audio|mp4|mpeg|mp2t|aac|ac3|eac3|octet-stream)(?:$|[+;])",
    re.I,
)
_LICENSE_PATH = re.compile(
    r"(?:license|licence|rightsmanager|widevine|playready|drm|\.prd(?:/|$))",
    re.I,
)
_MAX_PREVIEW = 16 * 1024
_MAX_LICENSE_PREVIEW = 64 * 1024
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _secret_name(name: object) -> bool:
    return bool(_SECRET_NAME.search(str(name or "").replace(" ", "-")))


def _manifest_url(url: str, content_type: str = "") -> bool:
    return bool(_MANIFEST_PATH.search(url or "") or re.search(r"(?:dash|mpegurl|m3u8|mpd)", content_type or "", re.I))


def _license_url(url: str) -> bool:
    return bool(_LICENSE_PATH.search(url or ""))


def _trace_url(url: str) -> bool:
    return not bool(_MEDIA_PATH.search(url or ""))


def redact_url(url: str, *, preserve_manifest: bool = False) -> str:
    """Redact credential-like query values while retaining useful URL shape."""
    value = str(url or "")
    if preserve_manifest or _manifest_url(value):
        return value
    try:
        parsed = urlsplit(value)
        query = []
        for name, item in parse_qsl(parsed.query, keep_blank_values=True):
            query.append((name, "<redacted>" if _SECRET_QUERY.match(name) else item))
        return urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))
    except ValueError:
        return value


def redact_headers(headers: Mapping[str, object] | None) -> dict[str, str]:
    return {
        str(name): "<redacted>" if _secret_name(name) else str(value)
        for name, value in (headers or {}).items()
    }


def _redact_value(value: Any, *, key: str = "") -> Any:
    if _secret_name(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, key=key) for item in value]
    if isinstance(value, str):
        # Catch bearer/JWT-like values even when a service put them in a free-form
        # error string rather than a JSON field.
        value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", value)
        value = re.sub(r"(?i)(\b(?:token|password|passwd|secret|sessionid|jsessionid)\s*[:=]\s*)[^,;\s]+", r"\1<redacted>", value)
    return value


def redact_text(value: str, *, limit: int = _MAX_PREVIEW) -> str:
    text = str(value or "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        cleaned = _redact_value(text)
    else:
        cleaned = json.dumps(_redact_value(parsed), ensure_ascii=False, indent=2)
    if len(cleaned) > limit:
        return cleaned[:limit] + f"\n… <truncated, {len(cleaned)} bytes total>"
    return cleaned


def _safe_log_text(value: object) -> str:
    """Redact credentials in free-form service logs, preserving signed manifests."""
    text = str(value or "")
    saved: dict[str, str] = {}

    def hold(match: re.Match[str]) -> str:
        url = match.group(0)
        if not _manifest_url(url):
            return url
        key = f"__UNIDL_SIGNED_MANIFEST_{len(saved)}__"
        saved[key] = url
        return key

    protected = _URL_IN_TEXT.sub(hold, text)
    cleaned = redact_text(protected, limit=10**9)
    for key, url in saved.items():
        cleaned = cleaned.replace(key, url)
    return cleaned


def _body_preview(body: object, *, limit: int) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            encoded = base64.b64encode(body).decode("ascii")
            return f"<base64 {len(body)} bytes> {encoded[:limit]}"
        return redact_text(text, limit=limit)
    if isinstance(body, (dict, list, tuple)):
        return redact_text(json.dumps(body, ensure_ascii=False), limit=limit)
    return redact_text(str(body), limit=limit)


def current_recorder() -> DebugRecorder | None:
    return _CURRENT.get()


@contextlib.contextmanager
def activate(recorder: DebugRecorder | None) -> Iterator[None]:
    token = _CURRENT.set(recorder)
    try:
        yield
    finally:
        _CURRENT.reset(token)


class DebugRecorder:
    """Thread-safe, timestamped diagnostic file writer."""

    def __init__(self, path: Path, *, session_id: str | None = None):
        self.path = Path(path)
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self._lock = threading.RLock()
        self._closed = False

    def log(self, message: object, *, event: str = "log") -> None:
        if self._closed:
            return
        text = _safe_log_text(message)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as file:
                    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines() or [""]:
                        file.write(f"[{_timestamp()}] [{self.session_id}] [{event}] {line}\n")
            except OSError:
                return

    def close(self) -> None:
        self._closed = True

    def record_request(self, method: str, url: str, headers: Mapping[str, object] | None, body: object = None) -> None:
        manifest = _manifest_url(url)
        shown_url = redact_url(url, preserve_manifest=manifest)
        self.log(f"{method.upper()} {shown_url}", event="api.request")
        self.log(f"request headers: {json.dumps(redact_headers(headers), ensure_ascii=False, sort_keys=True)}", event="api.request")
        if body is not None and _license_url(url):
            # License payloads are held back unless the request fails.  Keep the
            # value temporarily in the recorder only; do not write it here.
            return

    def record_response(
        self,
        method: str,
        url: str,
        response: Any = None,
        *,
        request_body: object = None,
        streamed: bool = False,
        elapsed: float | None = None,
        error: BaseException | None = None,
    ) -> None:
        status = (
            int(getattr(response, "status_code", getattr(response, "status", 0)) or 0)
            if response is not None
            else 0
        )
        headers = getattr(response, "headers", {}) if response is not None else {}
        content_type = str((headers or {}).get("Content-Type", ""))
        is_license = _license_url(url)
        failed = error is not None or status >= 400 or status == 0
        timing = f" {elapsed * 1000:.0f}ms" if elapsed is not None else ""
        self.log(
            f"{method.upper()} {redact_url(url, preserve_manifest=_manifest_url(url, content_type))} "
            f"→ {status or 'error'} ({content_type or 'unknown'}){timing}",
            event="api.response",
        )
        self.log(
            f"response headers: {json.dumps(redact_headers(headers), ensure_ascii=False, sort_keys=True)}",
            event="api.response",
        )
        if error is not None:
            self.log(f"error: {type(error).__name__}: {error}", event="api.response")
        # Do not consume media streams or dump successful license bodies.  API
        # responses and failed requests are the useful diagnostic evidence.
        is_media = bool(_MEDIA_PATH.search(url or "") or _MEDIA_CONTENT.search(content_type or ""))
        should_preview = (failed and not is_media) or (
            not failed
            and not is_license
            and not is_media
            and not _MANIFEST_PATH.search(url or "")
        )
        if response is not None and should_preview and not streamed:
            try:
                body = response.text
            except Exception as exc:  # pragma: no cover - unusual response object
                body = f"<response preview unavailable: {exc}>"
            self.log(
                f"response body: {_body_preview(body, limit=_MAX_LICENSE_PREVIEW if is_license and failed else _MAX_PREVIEW)}",
                event="api.response",
            )
        if is_license and failed:
            self.log(f"license challenge: {_body_preview(request_body, limit=_MAX_LICENSE_PREVIEW)}", event="license.failure")

    def record_exception(self, message: str, exc: BaseException) -> None:
        self.log(f"{message}: {type(exc).__name__}: {exc}", event="exception")

    def record_license_failure(
        self,
        challenge: object,
        response: object,
        error: BaseException,
    ) -> None:
        """Keep DRM payloads only for an exchange that actually failed."""
        self.log(
            f"license challenge: {_body_preview(challenge, limit=_MAX_LICENSE_PREVIEW)}",
            event="license.failure",
        )
        self.log(
            f"license response: {_body_preview(response, limit=_MAX_LICENSE_PREVIEW)}",
            event="license.failure",
        )
        self.record_exception("license exchange failed", error)


def install_requests_hook() -> None:
    """Install a no-op-unless-debug hook around requests.Session.request."""
    marker = "_unidl_debug_request_hook"
    if getattr(requests.sessions.Session.request, marker, False):
        return
    original = requests.sessions.Session.request

    def request(session, method, url, **kwargs):  # type: ignore[no-untyped-def]
        recorder = current_recorder()
        if recorder is None:
            return original(session, method, url, **kwargs)
        headers = dict(getattr(session, "headers", {}) or {})
        headers.update(kwargs.get("headers") or {})
        if getattr(session, "cookies", None) and "cookie" not in {str(key).lower() for key in headers}:
            headers["Cookie"] = "<redacted>"
        body = kwargs.get("data")
        if body is None:
            body = kwargs.get("json")
        trace = _trace_url(str(url))
        if trace:
            recorder.record_request(str(method), str(url), headers, body)
        started = time.monotonic()
        try:
            response = original(session, method, url, **kwargs)
        except Exception as exc:
            if trace:
                recorder.record_response(
                    str(method),
                    str(url),
                    None,
                    request_body=body,
                    elapsed=time.monotonic() - started,
                    error=exc,
                )
            raise
        if trace:
            recorder.record_response(
                str(method),
                str(url),
                response,
                request_body=body,
                streamed=bool(kwargs.get("stream", False)),
                elapsed=time.monotonic() - started,
            )
        return response

    setattr(request, marker, True)
    request.__wrapped__ = original
    requests.sessions.Session.request = request


def _install_httpx_hook() -> None:
    if _httpx is None:
        return
    marker = "_unidl_debug_request_hook"
    original = _httpx.Client.request
    if getattr(original, marker, False):
        return

    def request(client, method, url, **kwargs):  # type: ignore[no-untyped-def]
        recorder = current_recorder()
        if recorder is None:
            return original(client, method, url, **kwargs)
        text_url = str(url)
        headers = dict(getattr(client, "headers", {}) or {})
        headers.update(dict(kwargs.get("headers") or {}))
        if getattr(client, "cookies", None) and "cookie" not in {str(key).lower() for key in headers}:
            headers["Cookie"] = "<redacted>"
        body = kwargs.get("content")
        if body is None:
            body = kwargs.get("data") or kwargs.get("json")
        trace = _trace_url(text_url)
        if trace:
            recorder.record_request(str(method), text_url, headers, body)
        started = time.monotonic()
        try:
            response = original(client, method, url, **kwargs)
        except Exception as exc:
            if trace:
                recorder.record_response(
                    str(method), text_url, None, request_body=body,
                    elapsed=time.monotonic() - started, error=exc,
                )
            raise
        if trace:
            recorder.record_response(
                str(method), text_url, response, request_body=body,
                streamed=False, elapsed=time.monotonic() - started,
            )
        return response

    setattr(request, marker, True)
    request.__wrapped__ = original
    _httpx.Client.request = request


install_requests_hook()
_install_httpx_hook()

__all__ = [
    "DebugRecorder",
    "activate",
    "current_recorder",
    "install_requests_hook",
    "redact_headers",
    "redact_text",
    "redact_url",
    "safe_log_text",
]

safe_log_text = _safe_log_text

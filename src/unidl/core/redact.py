"""Mask account details in a log prepared for sharing.

Deliberately **not** applied to the log itself, nor to the exported command. Both
of those are working artefacts: a manifest URL is copied out of the log in order
to be used, and it stops working the moment its signature is removed; a command
file is meant to be run, and a command with its ``Authorization`` header masked
runs into a 401. Redacting in place would protect a secret by breaking the tool.

So this is what the *share* action puts on the clipboard, and the only thing that
uses it. What it masks:

* credentials in a URL - ``https://user:pass@host``
* query parameters that carry an authorisation of some kind, except on a
  ``manifest`` log line, where the signed URL must remain usable when copied
* header values in a command line: ``Authorization``, ``Cookie``, and friends
* JWTs outside the retained manifest URL
* email addresses, down to their first character
* the home directory, which is a person's name on most machines

What it leaves alone, on purpose: ``KID:key`` pairs and complete signed manifest
URLs on manifest log lines. Those are the things a report is usually *about*, and
a report that has had its subject removed is not one. Other credential-bearing
lines remain masked.
"""

from __future__ import annotations

import re
from pathlib import Path

MASK = "***"

#: Query parameters whose value authorises something. Matched case-insensitively
#: on the whole name, so ``keyword`` is not mistaken for ``key``.
SECRET_PARAMS = (
    "password",
    "passwd",
    "pwd",
    "pass",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "auth",
    "authorization",
    "api_key",
    "apikey",
    "key",
    "secret",
    "signature",
    "sig",
    "hdnts",
    "hdntl",
    "policy",
    "session",
    "sessionid",
    "jwt",
)

#: Headers whose value is a credential, as they appear in a command line.
SECRET_HEADERS = ("authorization", "cookie", "x-api-key", "x-auth-token", "set-cookie")

_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/\s:@]+):[^/\s@]+@")
_PARAM = re.compile(
    r"(?P<name>\b(?:" + "|".join(SECRET_PARAMS) + r"))(?P<sep>=)(?P<value>[^&\s\"'|>]+)",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"(?P<name>\b(?:" + "|".join(SECRET_HEADERS) + r"))(?P<sep>:\s*)(?P<value>[^'\"\n]+)",
    re.IGNORECASE,
)
#: three base64url segments, which is a JWT and nothing else
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
_EMAIL = re.compile(r"\b(?P<first>[A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@(?P<host>[A-Za-z0-9.-]+)\b")
_MANIFEST_FIELD = re.compile(
    r"^[ \t]*(?:●[ \t]+)?manifest(?:[ \t]*:[ \t]*|[ \t]+)(?P<url>https?://[^\s\"'|<>]+)",
    re.IGNORECASE,
)
_MANIFEST_WRAP = re.compile(r"[ \t]*\r?\n[ \t]+([^\s\"'|<>]+)[ \t]*(?=\r?\n|$)")
_MANIFEST_USERINFO = re.compile(r"^(https?://)[^/?#]+@", re.IGNORECASE)


def _manifest_url(text: str) -> tuple[int, int, str] | None:
    """Locate only the manifest field's URL, not URLs in ordinary messages."""
    field = _MANIFEST_FIELD.match(text)
    if field is None:
        return None
    end = field.end()
    parts = [field["url"]]
    while continuation := _MANIFEST_WRAP.match(text, end):
        token = continuation[1]
        name = re.split(r"[:=]", token, maxsplit=1)[0]
        if "://" in token or name.casefold() in (*SECRET_HEADERS, *SECRET_PARAMS):
            break
        parts.append(token)
        end = continuation.end()
    return field.start("url"), end, "".join(parts)


def _mask_userinfo(match: re.Match[str]) -> str:
    # the user name is kept: "which account was this" is a fair question, and the
    # password is the part that is not
    return f"{match.group('scheme')}{match.group('user')}:{MASK}@"


def redact(
    text: object,
    *,
    home: Path | None = None,
    preserve_manifest_urls: bool = False,
) -> str:
    """``text`` with credentials, tokens and the home path taken out."""
    body = str(text if text is not None else "")
    if not body:
        return body
    if preserve_manifest_urls and (manifest := _manifest_url(body)) is not None:
        start, end, url = manifest
        url = _MANIFEST_USERINFO.sub(r"\1" + MASK + "@", url)
        return redact(body[:start], home=home) + url + redact(body[end:], home=home)
    body = _USERINFO.sub(_mask_userinfo, body)
    body = _JWT.sub(MASK, body)
    body = _PARAM.sub(lambda m: f"{m.group('name')}{m.group('sep')}{MASK}", body)
    body = _HEADER.sub(lambda m: f"{m.group('name')}{m.group('sep')}{MASK}", body)
    body = _EMAIL.sub(lambda m: f"{m.group('first')}{MASK}@{m.group('host')}", body)
    return collapse_home(body, home=home)


def collapse_home(text: str, *, home: Path | None = None) -> str:
    """Replace the configured home directory with ``~``.

    Its own function because a path is worth collapsing in places where nothing
    else needs masking - a readiness report is all paths and no secrets.
    """
    root = str(home if home is not None else Path.home())
    return text.replace(root, "~") if root and root != "/" else text


def redact_lines(
    lines: object,
    *,
    home: Path | None = None,
    preserve_manifest_urls: bool = False,
) -> str:
    """A whole log, one line per row, masked and joined."""
    rows = [str(line) for line in (lines or ())]
    return "\n".join(
        redact(row, home=home, preserve_manifest_urls=preserve_manifest_urls)
        for row in rows
    )


__all__ = ["MASK", "SECRET_HEADERS", "SECRET_PARAMS", "collapse_home", "redact", "redact_lines"]

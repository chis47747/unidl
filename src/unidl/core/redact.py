"""Masking a log so it can be pasted somewhere public.

Deliberately **not** applied to the log itself, nor to the exported command. Both
of those are working artefacts: a manifest URL is copied out of the log in order
to be used, and it stops working the moment its signature is removed; a command
file is meant to be run, and a command with its ``Authorization`` header masked
runs into a 401. Redacting in place would protect a secret by breaking the tool.

So this is what the *share* action puts on the clipboard, and the only thing that
uses it. What it masks:

* credentials in a URL - ``https://user:pass@host``
* query parameters that carry an authorisation of some kind, which is what a
  signed CDN URL is made of
* header values in a command line: ``Authorization``, ``Cookie``, and friends
* anything shaped like a JWT, wherever it appears
* email addresses, down to their first character
* the home directory, which is a person's name on most machines

What it leaves alone, on purpose: ``KID:key`` pairs and the shape of a manifest
URL. Those are the two things a report is usually *about*, and a report that has
had its subject removed is not one. The action that calls this says so, so nobody
has to guess which half they are pasting.
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


def _mask_userinfo(match: re.Match[str]) -> str:
    # the user name is kept: "which account was this" is a fair question, and the
    # password is the part that is not
    return f"{match.group('scheme')}{match.group('user')}:{MASK}@"


def redact(text: object, *, home: Path | None = None) -> str:
    """``text`` with credentials, tokens and the home path taken out."""
    body = str(text if text is not None else "")
    if not body:
        return body
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


def redact_lines(lines: object, *, home: Path | None = None) -> str:
    """A whole log, one line per row, masked and joined."""
    rows = [str(line) for line in (lines or ())]
    return "\n".join(redact(row, home=home) for row in rows)


__all__ = ["MASK", "SECRET_HEADERS", "SECRET_PARAMS", "collapse_home", "redact", "redact_lines"]

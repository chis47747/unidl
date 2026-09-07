"""Credential model.

Services declare the slots they need; ``unidl.yaml`` fills them. Slots exist
because one service can need several unrelated logins and because field names
differ per service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def mask_account(value: object) -> str:
    """Shorten an account name for display: ``te***@outlook.com``.

    Applied by the UI to whatever a service reports, rather than trusting every
    service to remember. One guarantee in one place beats 150 conventions.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    head, at, domain = text.partition("@")
    if at:
        return f"{head[:2]}***@{domain}" if len(head) > 2 else f"{head}***@{domain}"
    return text


def mask_in(text: object) -> str:
    """Mask any email-looking substring inside a longer label."""
    import re

    return re.sub(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        lambda match: mask_account(match.group(0)),
        str(text or ""),
    )


@dataclass
class CredentialSlot:
    """Declares one login this service can use."""

    key: str
    label: str
    fields: tuple[str, ...] = ("username", "password")
    optional: bool = True
    help: str = ""


@dataclass
class Credential:
    """Resolved credential values for one slot."""

    slot: str
    values: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, fallback: str = "") -> Any:
        value = self.values.get(name, fallback)
        return value if value is not None else fallback

    @property
    def username(self) -> str:
        return str(self.get("username") or self.get("email") or "")

    @property
    def password(self) -> str:
        return str(self.get("password") or "")

    @property
    def cookies(self) -> str:
        return str(self.get("cookies") or "")

    @property
    def complete(self) -> bool:
        return bool(self.username and self.password) or bool(self.cookies)

    def masked(self) -> dict[str, str]:
        """Safe-to-display view. Never returns secret values."""
        out: dict[str, str] = {}
        for name, value in self.values.items():
            if not value:
                out[name] = "-"
            elif name in {"password", "token", "secret"}:
                out[name] = "*" * 8
            elif name in {"username", "email"}:
                text = str(value)
                head, _, domain = text.partition("@")
                out[name] = f"{head[:2]}***@{domain}" if domain else f"{head[:2]}***"
            else:
                out[name] = str(value)
        return out

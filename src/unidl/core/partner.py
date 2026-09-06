"""One-time authorization handoffs between otherwise isolated services."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

_SERVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")


class PartnerAuthorizationError(RuntimeError):
    """A partner handoff was invalid, expired, repeated, or refused."""


class PartnerAuthorization:
    """A short-lived URL which can be claimed exactly once.

    The URL is deliberately absent from ``repr`` and cannot be pickled. Reading
    :attr:`url` claims and erases it, so the producer cannot log or cache it after
    handing it to core and a receiver cannot accidentally submit it twice.
    """

    __slots__ = (
        "_claimed",
        "_created_at",
        "_host",
        "_lock",
        "_max_age",
        "_provider",
        "_service_id",
        "_source_service_id",
        "_url",
    )

    def __init__(
        self,
        url: str,
        *,
        source_service_id: str,
        service_id: str,
        provider: str = "",
        max_age: float = 300.0,
    ) -> None:
        target = str(url or "").strip()
        parsed = urlsplit(target)
        host = (parsed.hostname or "").lower().rstrip(".")
        source = str(source_service_id or "").strip().lower()
        destination = str(service_id or "").strip().lower()
        if parsed.scheme.lower() != "https" or not host or parsed.port not in {None, 443}:
            raise PartnerAuthorizationError("partner authorization requires an HTTPS URL")
        if not _SERVICE_ID.fullmatch(source):
            raise PartnerAuthorizationError("partner authorization has an invalid source service id")
        if not _SERVICE_ID.fullmatch(destination):
            raise PartnerAuthorizationError("partner authorization has an invalid target service id")
        if max_age <= 0:
            raise PartnerAuthorizationError("partner authorization lifetime must be positive")

        self._url = target
        self._created_at = time.monotonic()
        self._max_age = float(max_age)
        self._claimed = False
        self._lock = threading.Lock()
        self._source_service_id = source
        self._service_id = destination
        self._provider = str(provider or destination).strip()
        self._host = host

    @property
    def source_service_id(self) -> str:
        return self._source_service_id

    @property
    def service_id(self) -> str:
        return self._service_id

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def host(self) -> str:
        return self._host

    @property
    def url(self) -> str:
        """Claim and erase the URL, refusing expired or repeated reads."""
        with self._lock:
            if self._claimed or not self._url:
                raise PartnerAuthorizationError("partner authorization was already consumed")
            if time.monotonic() - self._created_at > self._max_age:
                self._claimed = True
                self._url = ""
                raise PartnerAuthorizationError("partner authorization expired before it was consumed")
            target = self._url
            self._url = ""
            self._claimed = True
            return target

    @property
    def claimed(self) -> bool:
        return self._claimed

    def discard(self) -> None:
        """Erase an unclaimed URL when routing fails before a receiver reads it."""
        with self._lock:
            self._url = ""
            self._claimed = True

    def __repr__(self) -> str:
        state = "consumed" if self._claimed else "pending"
        return (
            "PartnerAuthorization("
            f"source_service_id={self.source_service_id!r}, "
            f"service_id={self.service_id!r}, provider={self.provider!r}, "
            f"host={self.host!r}, url=<redacted>, state={state!r})"
        )

    def __reduce__(self):
        raise TypeError("PartnerAuthorization cannot be serialized")


@dataclass(frozen=True)
class PartnerAuthorizationResult:
    """Non-sensitive result returned from a receiver to the producer flow."""

    service_id: str
    authenticated: bool
    label: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.authenticated


class PartnerAuthorizationReceiver(Protocol):
    ID: str

    @classmethod
    def accepts_partner_authorization(cls, source_service_id: str) -> bool: ...

    def consume_partner_authorization(
        self, authorization: PartnerAuthorization
    ) -> PartnerAuthorizationResult: ...


def deliver_partner_authorization(
    authorization: PartnerAuthorization,
    receiver: PartnerAuthorizationReceiver,
) -> PartnerAuthorizationResult:
    """Validate and synchronously deliver one handoff, always erasing its URL."""
    try:
        if receiver.ID != authorization.service_id:
            raise PartnerAuthorizationError(
                f"partner authorization targets {authorization.service_id}, not {receiver.ID}"
            )
        if not receiver.accepts_partner_authorization(authorization.source_service_id):
            raise PartnerAuthorizationError(
                f"{receiver.ID} does not accept authorization from "
                f"{authorization.source_service_id}"
            )
        result = receiver.consume_partner_authorization(authorization)
        if not isinstance(result, PartnerAuthorizationResult):
            raise PartnerAuthorizationError(
                f"{receiver.ID} returned an invalid partner authorization result"
            )
        return result
    finally:
        authorization.discard()


__all__ = [
    "PartnerAuthorization",
    "PartnerAuthorizationError",
    "PartnerAuthorizationResult",
    "deliver_partner_authorization",
]

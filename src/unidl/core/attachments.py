"""Optional service-owned files associated with a playback title."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from .secureio import safe_filename


@dataclass(frozen=True, slots=True)
class Attachment:
    """One independently downloadable title attachment."""

    url: str
    name: str = ""
    kind: str = "image"
    mime_type: str = ""
    filename: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        url = str(self.url or "").strip()
        if not url:
            raise ValueError("attachment url cannot be empty")
        name = " ".join(str(self.name or "").split())
        kind = " ".join(str(self.kind or "image").split()).casefold() or "image"
        mime = str(self.mime_type or "").strip().casefold()
        filename = str(self.filename or "").strip()
        if filename:
            filename = safe_filename(PurePath(filename).name, label="attachment filename")
        headers = {
            str(key): str(value)
            for key, value in dict(self.headers or {}).items()
            if str(key).strip() and str(value).strip()
        }
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "name", name or kind.title())
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "mime_type", mime)
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "headers", headers)

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/") or self.kind in {
            "image", "poster", "thumb", "thumbnail", "artwork"
        }

    def as_document(self) -> dict[str, Any]:
        data: dict[str, Any] = {"url": self.url, "name": self.name, "kind": self.kind}
        if self.mime_type:
            data["mime_type"] = self.mime_type
        if self.filename:
            data["filename"] = self.filename
        if self.headers:
            data["headers"] = dict(self.headers)
        return data

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> Attachment:
        if not isinstance(value, Mapping):
            raise TypeError("attachment document must be an object")
        headers = value.get("headers")
        return cls(
            url=str(value.get("url") or value.get("source") or ""),
            name=str(value.get("name") or value.get("label") or ""),
            kind=str(value.get("kind") or "image"),
            mime_type=str(value.get("mime_type") or value.get("mime") or ""),
            filename=str(value.get("filename") or ""),
            headers=headers if isinstance(headers, Mapping) else {},
        )


def normalize_attachments(values: object) -> tuple[Attachment, ...]:
    result: list[Attachment] = []
    seen: set[tuple[str, str, str]] = set()
    for value in values or ():
        attachment = value if isinstance(value, Attachment) else Attachment.from_document(value)
        identity = (attachment.url, attachment.name, attachment.kind)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(attachment)
    return tuple(result)


def count_label(attachments: object) -> str:
    from .i18n import tr

    count = len(tuple(attachments or ()))
    return tr("attachments.count.one", count=count) if count == 1 else tr("attachments.count.other", count=count)


__all__ = ["Attachment", "count_label", "normalize_attachments"]

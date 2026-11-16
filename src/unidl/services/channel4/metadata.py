"""Artwork explicitly returned in Channel 4's TV catalogue and playback data."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ...core.attachments import Attachment

_IMAGE_FIELDS = ("href", "title", "type", "templated", "width", "height")
_MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".avif": "image/avif", ".svg": "image/svg+xml",
}


def image_fields(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    images = [raw.get("image")]
    if isinstance(raw.get("images"), list):
        images.extend(raw["images"])
    return {"images": [
        {key: image[key] for key in _IMAGE_FIELDS if key in image}
        for image in images if isinstance(image, dict)
    ]}


def attachments(*documents: dict[str, Any]) -> list[Attachment]:
    result: list[Attachment] = []
    seen: set[str] = set()
    for document in documents:
        for group, label, kind in (
            ("episode", "Episode image", "thumbnail"),
            ("brand", "Programme artwork", "artwork"),
            ("channel", "Channel artwork", "artwork"),
        ):
            section = document.get(group)
            if not isinstance(section, dict) or not isinstance(section.get("images"), list):
                continue
            for image in section["images"]:
                if not isinstance(image, dict):
                    continue
                value = image.get("href")
                if not isinstance(value, str):
                    continue
                url = value.strip()
                try:
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None
                        or any(char.isspace() or ord(char) < 32 for char in url)
                        or any(char in url for char in "{}")
                    ):
                        continue
                except ValueError:
                    continue
                if url in seen:
                    continue
                mime = str(image.get("type") or "").lower()
                if mime and not mime.startswith("image/"):
                    continue
                if not mime:
                    mime = _MIME_TYPES.get("." + parsed.path.rsplit(".", 1)[-1].lower(), "")
                image_kind = kind
                width, height = image.get("width"), image.get("height")
                if type(width) in (int, float) and type(height) in (int, float) and height > width > 0:
                    image_kind = "poster"
                title = image.get("title")
                name = title if isinstance(title, str) and title.strip() else label
                result.append(Attachment(url=url, name=name, kind=image_kind, mime_type=mime))
                seen.add(url)
    return result

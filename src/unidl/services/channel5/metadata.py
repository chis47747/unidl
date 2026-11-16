"""Channel 5 artwork from the Android app's Otis image contract."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from ...core.attachments import Attachment

_IMAGES = "https://api-images.channel5.com/otis/images"


def attachments(raw: dict[str, Any], *, live: bool = False) -> list[Attachment]:
    candidates: list[tuple[Any, str, str]] = []
    images = raw.get("imageUrls")
    images = images if isinstance(images, dict) else {}
    if live:
        for value in (raw.get("channel_logo"), images.get("channelBrandLogo")):
            candidates.append((value, "Channel logo", "artwork"))
    else:
        candidates.extend((
            (raw.get("episodeImage"), "Episode image", "thumbnail"),
            (raw.get("showImage"), "Show artwork", "artwork"),
        ))
        # The app builds these URLs from Corona IDs; Corona need not embed URLs.
        item_id = raw.get("id")
        show_id = raw.get("sh_id")
        if isinstance(item_id, str) and item_id:
            candidates.append((
                _image("episode", item_id, "1024x576", raw.get("img_upd")),
                "Episode image", "thumbnail",
            ))
        if isinstance(show_id, str) and show_id:
            candidates.append((_image("show", show_id, "1920x1080"), "Show artwork", "artwork"))

    result: list[Attachment] = []
    seen: set[str] = set()
    for value, name, kind in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        url = value.strip()
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(char.isspace() for char in url)
            ):
                continue
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
            parsed.path.rsplit(".", 1)[-1].lower(), ""
        )
        result.append(Attachment(url=url, name=name, kind=kind, mime_type=mime))
    return result


def _image(kind: str, identity: str, resolution: str, updated: Any = None) -> str:
    params = {"overlay": "false", "master": "master"}
    if type(updated) is int and updated > 0:
        params["updated_at"] = str(updated)
    return f"{_IMAGES}/{kind}/{quote(identity, safe='')}/{resolution}.jpg?{urlencode(params)}"

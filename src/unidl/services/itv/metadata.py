"""Artwork URLs supplied by ITV's TV catalogue and live-channel API."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlencode, urlsplit

from ...core.attachments import Attachment


def image_url(value: str) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char.isspace() for char in url)
        ):
            return ""
        if "{" not in url and "}" not in url:
            return url
        # ITV's TV image mapper and fullScreen preset (3.748.0 vendor bundle).
        options = {
            "width": "1920", "quality": "80", "blur": "0", "bg": "false",
            "image_format": "jpg", "aspect_ratio": "16x9",
            "class": "04_DesktopCTV_RailTileLandscape", "treatment": "title",
            "fallback": "standard", "distributionPartner": "",
        }
        if parsed.hostname == "ovp.itv.com":
            resource = parsed.path.split("/")[3]
            if resource == "episode":
                options["class"] = "08_Episodic"
            elif resource in {"programme", "film", "special"}:
                options["class"] = "01_Hero_DesktopCTV"
            options["distributionPartner"] = "itv_hub"
        elif parsed.hostname != "images.ctfassets.net":
            return ""
        query = urlencode([
            (key, item.format_map(options))
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if (key, item) != ("h", "{height}")
            and not (parsed.hostname == "ovp.itv.com" and (key, item) == ("treatment", "{treatment}"))
        ])
        return parsed._replace(path=parsed.path.format_map(options), query=query).geturl()
    except (ValueError, KeyError, IndexError):
        return ""


def attachments(*images: tuple[str, str, str]) -> list[Attachment]:
    result: list[Attachment] = []
    seen: set[str] = set()
    for value, label, kind in images:
        url = image_url(value)
        if not url or url in seen:
            continue
        seen.add(url)
        parsed = urlsplit(url)
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".avif": "image/avif",
        }.get(PurePosixPath(parsed.path).suffix.lower(), "")
        result.append(Attachment(url=url, name=label, kind=kind, mime_type=mime))
    return result

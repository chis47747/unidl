"""Audio-only presentation helpers for the TUI.

The download core owns ID3 data and the service owns the cover URL.  This module
is deliberately only a presenter: it fetches a bounded preview image off the UI
thread, renders it as a small true-colour terminal image, and never changes the
audio metadata passed to the native downloader.
"""

from __future__ import annotations

import base64
import binascii
import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import requests
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from ..core.i18n import tr
from ..core.playback import Playback
from .qr import halfcell_image_class, protocol_cell_size, protocol_image_class

if TYPE_CHECKING:
    from PIL import Image

MAX_COVER_BYTES = 8 * 1024 * 1024
MAX_COVER_PIXELS = 16 * 1024 * 1024
# The fallback paints each terminal cell with a background colour.  This is
# intentionally a space rather than a block glyph: block glyphs only occupy the
# font's ink box, leaving a regular gap between rows in Terminal.app.  A cell
# background fills the complete terminal cell.  Terminal cells are roughly
# twice as tall as they are wide, so a 36 x 18 cell raster is displayed as a
# square while remaining reliable on plain ANSI terminals.
COVER_WIDTH = 36
COVER_HEIGHT = 18
MIN_COVER_WIDTH = 8
MIN_COVER_HEIGHT = 4
MAX_METADATA_ROWS = 8

_TAG_ROWS = (
    ("title", "audio.tag.title"),
    ("artist", "audio.tag.artist"),
    ("album", "audio.tag.album"),
    ("album_artist", "audio.tag.album_artist"),
    ("date", "audio.tag.date"),
    ("track", "audio.tag.track"),
    ("disc", "audio.tag.disc"),
    ("genre", "audio.tag.genre"),
    ("composer", "audio.tag.composer"),
    ("publisher", "audio.tag.publisher"),
    ("copyright", "audio.tag.copyright"),
    ("isrc", "audio.tag.isrc"),
    ("comment", "audio.tag.comment"),
)


@dataclass(frozen=True)
class AudioPreview:
    """The non-sensitive audio presentation data needed by the TUI."""

    tags: Mapping[str, Any] = field(default_factory=dict)
    cover_source: str = ""
    cover_headers: Mapping[str, str] = field(default_factory=dict)
    proxy: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", _normalise_preview_tags(self.tags))

    @classmethod
    def from_value(
        cls,
        value: AudioPreview | Mapping[str, Any] | None,
    ) -> AudioPreview | None:
        """Convert a service-owned picker preview into the TUI model."""

        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("audio preview must be an AudioPreview or mapping")
        tags = value.get("tags")
        headers = value.get("cover_headers")
        return cls(
            tags=tags if isinstance(tags, Mapping) else {},
            cover_source=str(value.get("cover_source") or ""),
            cover_headers=headers if isinstance(headers, Mapping) else {},
            proxy=str(value.get("proxy")) if value.get("proxy") else None,
        )

    @classmethod
    def from_playback(cls, playback: Playback) -> AudioPreview:
        # A service may add only one or two explicit fields (most commonly a
        # signed cover) while the public Title still carries the ordinary ID3
        # defaults.  Merge those sources so a partial ``audio_tags`` payload
        # does not make the rest of the metadata disappear from the preview.
        title_tags = (
            playback.title.audio_tags() if playback.title is not None else {}
        )
        tags = _normalise_preview_tags(title_tags)
        for key, value in _normalise_preview_tags(playback.audio_tags).items():
            if value not in (None, ""):
                tags[key] = value
        cover = tags.get("cover")
        source = ""
        # Do not forward manifest Authorization/Cookie headers to an unrelated
        # artwork host.  Services that need headers for their cover URL put them
        # explicitly in ``audio_tags['cover']['headers']``.
        headers: dict[str, str] = {}
        if isinstance(cover, dict):
            source = str(cover.get("path") or cover.get("url") or "").strip()
            supplied = cover.get("headers")
            if isinstance(supplied, Mapping):
                headers.update(
                    {
                        str(key): str(value)
                        for key, value in supplied.items()
                        if str(key).strip() and str(value).strip()
                    }
                )
        elif isinstance(cover, str):
            source = cover.strip()
        if not source:
            source = str(getattr(playback.title, "cover_url", "") or "").strip()
        return cls(
            tags=tags,
            cover_source=source,
            cover_headers=headers,
            proxy=playback.proxy,
        )


def _normalise_preview_tags(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Flatten common ID3 aliases so the preview matches the saved sidecar."""

    raw = dict(value or {})
    nested = raw.get("audio_metadata")
    if isinstance(nested, Mapping):
        raw = {**raw, **nested}
    aliases = {
        "name": "title",
        "albumArtist": "album_artist",
        "album_artist": "album_artist",
        "trackNumber": "track",
        "track_number": "track",
        "discNumber": "disc",
        "disc_number": "disc",
        "releaseDate": "date",
        "release_date": "date",
        "ISRC": "isrc",
    }
    for source, target in aliases.items():
        if target not in raw and raw.get(source) not in (None, ""):
            raw[target] = raw[source]
    if "cover" not in raw:
        for source in ("coverUrl", "cover_url"):
            cover = raw.get(source)
            if cover not in (None, ""):
                raw["cover"] = {"url": cover} if isinstance(cover, str) else cover
                break
    return raw


def _text_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    if value is None or isinstance(value, (dict, set)):
        return ""
    return str(value).strip()


def _cover_bytes(source: str, headers: Mapping[str, str], proxy: str | None) -> bytes:
    """Read one bounded cover image source without blocking the UI thread."""

    source = str(source or "").strip()
    if not source:
        raise ValueError("no album cover was supplied")
    if source.lower().startswith("data:image/"):
        header, separator, encoded = source.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ValueError("album cover data URI is not base64 encoded")
        try:
            data = base64.b64decode("".join(encoded.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("album cover data URI is invalid") from exc
    else:
        parsed = urlsplit(source)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            kwargs: dict[str, Any] = {
                "headers": dict(headers),
                "timeout": (3.0, 6.0),
                "stream": True,
            }
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            with requests.get(source, **kwargs) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_COVER_BYTES:
                    raise ValueError("album cover is too large")
                data = response.raw.read(MAX_COVER_BYTES + 1)
        else:
            path = Path(unquote(parsed.path) if parsed.scheme == "file" else source).expanduser()
            data = path.read_bytes()
    if not data:
        raise ValueError("album cover is empty")
    if len(data) > MAX_COVER_BYTES:
        raise ValueError("album cover is too large")
    return data


def _open_cover(data: bytes) -> Image.Image:
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_COVER_PIXELS:
            raise ValueError("album cover dimensions are outside the safety limit")
        image.load()
        return image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("album cover dimensions"):
            raise
        raise ValueError("album cover is not a supported image") from exc


def _hex(pixel: tuple[int, int, int]) -> str:
    red, green, blue = (max(0, min(255, int(value))) for value in pixel)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _cover_pixels(
    image: Image.Image,
    width: int = COVER_WIDTH,
    height: int = COVER_HEIGHT,
) -> Text:
    """Render cover art as solid true-colour terminal cells.

    A coloured space fills the complete terminal cell, unlike ``█`` which only
    fills the font's ink box and leaves dark inter-row gaps.  The non-square
    raster compensates for the terminal cell's taller aspect ratio; unlike a
    crop-to-fit operation it also keeps the complete album artwork.
    """

    from PIL import Image

    width = max(MIN_COVER_WIDTH, int(width))
    height = max(MIN_COVER_HEIGHT, int(height))
    fitted = image.convert("RGB").resize(
        (width, height),
        Image.Resampling.LANCZOS,
    )
    pixels = fitted.load()
    result = Text(no_wrap=True, overflow="crop")
    for top in range(height):
        for left in range(width):
            result.append(
                " ",
                style=Style(bgcolor=_hex(pixels[left, top])),
            )
        if top + 1 < height:
            result.append("\n")
    return result


def _cover_renderable(
    image: Image.Image,
    width: int = COVER_WIDTH,
    height: int = COVER_HEIGHT,
):
    """Return the highest-detail renderer available in this installation.

    The half-cell renderer receives a square 36×36 pixel raster and displays
    it in 36×18 terminal cells.  Its ``height`` argument is the number of
    terminal rows (the renderer doubles that internally), so passing 36 here
    would generate 36 rows and clip the bottom half in our 18-row widget.  It
    therefore keeps twice the vertical detail of the plain-cell fallback
    without increasing the side rail width.  The fallback remains deliberately
    self-contained for environments that install
    UniDL without optional ``textual-image`` extras.
    """

    halfcell_image = halfcell_image_class()
    if halfcell_image is not None:
        return halfcell_image(
            image,
            width=width,
            height=height,
        )
    return _cover_pixels(image, width=width, height=height)


def _metadata_text(preview: AudioPreview, palette) -> Text:
    result = Text(no_wrap=False, overflow="fold")
    for key, label in _TAG_ROWS:
        value = _text_value(preview.tags.get(key))
        if not value:
            continue
        if result:
            result.append("\n")
        result.append(f"{tr(label)}: ", style=palette.muted)
        result.append(value, style=palette.fg2)
    if not result:
        result.append(tr("audio.no_metadata"), style=palette.dim)
    return result


class AudioPreviewWidget(Vertical):
    """Album cover and ID3 summary used beside audio track/progress panes."""

    def __init__(
        self,
        preview: AudioPreview | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.preview = AudioPreview.from_value(preview)
        self._load_token = 0

    def compose(self) -> ComposeResult:
        yield Static(tr("audio.cover"), classes="audio-cover-label")
        protocol_image = protocol_image_class()
        if protocol_image is not None:
            # Known Kitty/Sixel terminals can display the original pixels.  A
            # blank native widget is preferable to first painting a low-detail
            # fallback and then replacing it once the image arrives.
            yield protocol_image(id="audio-cover", classes="audio-cover")
        else:
            yield Static(tr("audio.cover_loading"), id="audio-cover", classes="audio-cover")
        yield Static("", id="audio-metadata", classes="audio-metadata")

    def on_mount(self) -> None:
        self._apply_layout()
        if self.preview is not None:
            self._refresh_preview()

    def on_resize(self) -> None:
        self._apply_layout()

    def _tag_count(self) -> int:
        if self.preview is None:
            return 0
        return sum(bool(_text_value(self.preview.tags.get(key))) for key, _label in _TAG_ROWS)

    def _cover_layout(self) -> tuple[int, int, int]:
        """Return cover width/height and the metadata rows left below it."""

        width = max(1, int(self.size.width))
        height = max(1, int(self.size.height))
        # Reserve about one third of a short card for a scrollable ID3 viewport;
        # the remaining rows keep a complete, aspect-correct cover visible.
        available = max(1, height - 2)  # label plus metadata top padding
        desired_metadata = (
            min(MAX_METADATA_ROWS + 1, max(4, self._tag_count() + 1))
            if self.preview
            else 1
        )
        metadata_rows = min(
            desired_metadata,
            max(1, available - MIN_COVER_HEIGHT),
            max(3, available // 3),
        )
        max_cover_height = min(
            COVER_HEIGHT,
            max(MIN_COVER_HEIGHT, available - metadata_rows),
        )
        cell_width, cell_height = protocol_cell_size() or (1, 2)
        cell_ratio = cell_height / cell_width
        cover_width = min(
            COVER_WIDTH,
            max(MIN_COVER_WIDTH, width - 1),
            max(MIN_COVER_WIDTH, round(max_cover_height * cell_ratio)),
        )
        # A half-cell renderer is symmetric only when its width is even.  Keeping
        # one column inside the measured content width prevents Textual/Rich from
        # cropping the final module on cards with borders and padding.
        cover_width = max(MIN_COVER_WIDTH, cover_width - cover_width % 2)
        cover_height = min(
            max_cover_height,
            max(MIN_COVER_HEIGHT, round(cover_width / cell_ratio)),
        )
        metadata_rows = max(1, height - 1 - cover_height)
        return cover_width, cover_height, metadata_rows

    def _apply_layout(self) -> None:
        try:
            label = self.query_one(".audio-cover-label")
            cover = self.query_one("#audio-cover")
            metadata = self.query_one("#audio-metadata", Static)
        except Exception:
            return
        available_height = max(1, int(self.size.height))
        # A four-row cover cannot coexist with the cover label and even one
        # metadata row in a very short terminal card.  Let ID3 remain useful in
        # that case; the cover is restored automatically on the next resize.
        compact = available_height < MIN_COVER_HEIGHT + 2
        label.display = available_height >= 2
        cover.display = not compact
        cover_width, cover_height, metadata_rows = self._cover_layout()
        if compact:
            metadata_rows = max(1, available_height - (1 if label.display else 0))
        cover.styles.width = cover_width
        cover.styles.height = cover_height
        metadata.styles.height = metadata_rows
        metadata.styles.max_height = metadata_rows
        metadata.styles.min_height = 1
        self._cover_size = (cover_width, cover_height)

    def set_preview(
        self,
        preview: AudioPreview | Mapping[str, Any] | None,
    ) -> None:
        self.preview = AudioPreview.from_value(preview)
        self._load_token += 1
        if not self.is_mounted:
            return
        self._apply_layout()
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        preview = self.preview
        cover = self.query_one("#audio-cover")
        if preview is None:
            if isinstance(cover, Static):
                cover.update(tr("audio.no_preview"))
            else:
                cover.image = None
            self.query_one("#audio-metadata", Static).update("")
            return
        self._apply_layout()
        if isinstance(cover, Static):
            cover.update(tr("audio.cover_loading"))
        else:
            cover.image = None
        self.query_one("#audio-metadata", Static).update(
            _metadata_text(preview, self.app.palette)
        )
        self._load_cover(self._load_token, preview)

    @work(thread=True, exclusive=True)
    def _load_cover(self, token: int, preview: AudioPreview) -> None:
        try:
            image = _open_cover(
                _cover_bytes(preview.cover_source, preview.cover_headers, preview.proxy)
            )
        except Exception:
            self.app.call_from_thread(self._show_cover, token, None)
            return
        self.app.call_from_thread(self._show_cover, token, image)

    def _show_cover(self, token: int, image: Image.Image | None) -> None:
        if token != self._load_token or not self.is_mounted:
            return
        target = self.query_one("#audio-cover")
        if image is None:
            if isinstance(target, Static):
                target.update(tr("audio.cover_unavailable"), layout=False)
            else:
                target.image = None
            return
        if protocol_image_class() is not None and not isinstance(target, Static):
            target.image = image
        else:
            width, height = getattr(self, "_cover_size", (COVER_WIDTH, COVER_HEIGHT))
            target.update(_cover_renderable(image, width=width, height=height), layout=False)

    def on_unmount(self) -> None:
        """Release a native terminal image before the preview leaves the DOM."""

        try:
            target = self.query_one("#audio-cover")
        except Exception:
            return
        if protocol_image_class() is not None and not isinstance(target, Static):
            target.image = None


__all__ = [
    "AudioPreview",
    "AudioPreviewWidget",
    "_cover_bytes",
    "_cover_pixels",
    "_cover_renderable",
]

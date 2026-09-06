"""Safe, presentation-only QR data shared by service flows and the TUI.

Services own authentication and network access.  This module owns only the two
things a presenter needs after a service has obtained a challenge: either the
text encoded by a QR code, or an already rendered image returned as bytes/data
URI or a provider image URL.  Keeping it out of ``Await.lines`` is deliberate --
an image-sized base64 value is neither useful log output nor something a user
should have to copy.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import io
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from PIL import Image

MAX_IMAGE_BYTES = 4 * 1024 * 1024
"""Largest accepted in-memory QR image."""

MAX_IMAGE_PIXELS = 16 * 1024 * 1024
"""Bound decompression before a service response reaches the renderer."""

MAX_REMOTE_IMAGE_REDIRECTS = 3
"""Maximum number of HTTPS redirects followed for an official QR image."""

MAX_REMOTE_IMAGE_ATTEMPTS = 2
"""Retry one interrupted CDN response before falling back to the payload."""

REMOTE_IMAGE_TIMEOUT = 4.0
"""Short timeout so a QR presenter never waits indefinitely on an image CDN."""

QUIET_ZONE = 4


class QrError(ValueError):
    """A QR presentation could not be decoded safely."""


Matrix = tuple[tuple[bool, ...], ...]


@dataclass(frozen=True)
class QrPresentation:
    """One QR challenge, independent of any particular terminal renderer.

    ``image_data`` and ``image_url`` are preferred when supplied: some providers
    return an official QR bitmap whose signing/quiet-zone/logo parameters are
    part of their login contract.  ``payload`` is then the reliable fallback and
    is also used when no provider image exists.  ``image_data`` is for services
    such as Migu which return only a PNG/JPEG data URI.

    ``fallback_url`` is the address a presenter may open if the terminal cannot
    show the code.  It is intentionally separate from the image data so a base64
    blob is never mistaken for a browser URL.
    """

    payload: str = ""
    image_data: str | bytes = b""
    fallback_url: str = ""
    alt: str = "QR code"
    image_url: str = ""

    def __post_init__(self) -> None:
        if (
            not str(self.payload or "").strip()
            and not self.image_data
            and not _is_https_url(str(self.image_url or "").strip())
            and not _is_remote_image_url(str(self.fallback_url or "").strip())
        ):
            raise QrError("QR presentation needs a payload, image data, or official image URL")

    @property
    def open_url(self) -> str:
        """A safe browser fallback, never an image data URI."""

        explicit = str(self.fallback_url or "").strip()
        if explicit.startswith(("http://", "https://")):
            return explicit
        payload = str(self.payload or "").strip()
        return payload if payload.startswith(("http://", "https://")) else ""

    def image(self, *, generated_canvas: tuple[int, int] | None = None) -> Image.Image:
        """Return a fully loaded RGB image suitable for a terminal protocol.

        ``generated_canvas`` applies only when ``payload`` has to be encoded
        locally.  Terminal image protocols reserve whole character cells, whose
        pixel aspect ratio is rarely square.  Drawing the QR as square integer
        modules inside that exact rectangular canvas prevents the protocol
        renderer from stretching or interpolating the code.  Provider bitmaps
        remain authoritative and are returned unchanged.
        """

        # An API-provided bitmap is authoritative.  It can contain provider-
        # specific margins or an embedded logo that a freshly encoded payload
        # loses.  A bad/stale CDN response must not make a valid payload unusable.
        if self.image_data:
            try:
                return _open_image(_image_bytes(self.image_data))
            except QrError:
                if not str(self.payload or "").strip():
                    raise

        remote_url = self._image_source_url()
        if remote_url:
            try:
                return _open_image(_download_image(remote_url))
            except QrError:
                if not str(self.payload or "").strip():
                    raise

        payload = str(self.payload or "").strip()
        if payload:
            return _generated_image(payload, canvas=generated_canvas)
        raise QrError("QR presentation has no usable image or payload")

    def matrix(self) -> Matrix:
        """Return exact light/dark modules for the terminal-text fallback."""

        # Prefer the provider-rendered image whenever one exists.  This matters
        # on Terminal.app, where the Unicode fallback is the only renderer: a
        # provider may have chosen a larger QR version, quiet zone, or logo-safe
        # margin than a local re-encoding of its scan URL.
        if self.image_data or self._image_source_url():
            try:
                return _matrix_from_image(self.image())
            except QrError:
                if not str(self.payload or "").strip():
                    raise

        payload = str(self.payload or "").strip()
        if payload:
            import qrcode

            code = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                border=QUIET_ZONE,
                box_size=1,
            )
            code.add_data(payload)
            code.make(fit=True)
            return tuple(tuple(bool(cell) for cell in row) for row in code.get_matrix())
        return _matrix_from_image(self.image())

    def _image_source_url(self) -> str:
        """Return an official QR image URL without changing browser fallback semantics.

        New callers should use ``image_url``.  Older service adapters historically
        placed the provider bitmap URL in ``fallback_url``; recognising only URLs
        with an unmistakable QR/image shape keeps those adapters working while
        never downloading an ordinary activation or scan URL by accident.
        """

        explicit = str(self.image_url or "").strip()
        if _is_https_url(explicit):
            return explicit
        legacy = str(self.fallback_url or "").strip()
        return legacy if _is_remote_image_url(legacy) else ""


def _image_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        raw = value
    else:
        text = str(value or "").strip()
        header, separator, encoded = text.partition(",")
        if not separator or not header.lower().startswith("data:image/") or ";base64" not in header.lower():
            raise QrError("QR image must be image bytes or a base64 image data URI")
        try:
            raw = base64.b64decode("".join(encoded.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise QrError("QR image data URI contains invalid base64") from exc
    if not raw:
        raise QrError("QR image is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise QrError("QR image is larger than the 4 MiB safety limit")
    return raw


# Do not treat every ``/qr-login`` or ``/qr`` activation page as an image: that
# would add a needless network timeout before the payload fallback.  These are
# the provider endpoint names used by the ported TV clients; ordinary image
# extensions are handled separately below.
_QR_IMAGE_PATH = re.compile(r"(?:qrcode|qrencode|gen_qrpic)", re.IGNORECASE)
_QR_IMAGE_EXT = re.compile(r"\.(?:png|jpe?g|webp|gif|bmp)(?:$|[?#])", re.IGNORECASE)


def _is_remote_image_url(value: str) -> bool:
    """Recognise an HTTPS provider QR bitmap URL conservatively."""

    if not _is_https_url(value):
        return False
    parsed = urlsplit(str(value or "").strip())
    path = parsed.path or ""
    query = parsed.query.lower()
    if _QR_IMAGE_PATH.search(path) or _QR_IMAGE_EXT.search(path):
        return True
    # Several TV APIs expose a generic endpoint but include the QR content and
    # requested dimensions in the query (for example ``?size=400&content=...``).
    return ("size=" in query or "width=" in query) and any(
        marker in query for marker in ("url=", "content=", "qrcode=", "qr=")
    )


def _is_https_url(value: str) -> bool:
    """Return whether a URL is an absolute HTTPS URL with a host."""

    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and bool(parsed.netloc)


class _HttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow only a small number of HTTPS redirects for QR image CDNs."""

    max_redirections = MAX_REMOTE_IMAGE_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        try:
            parsed = urlsplit(newurl)
        except ValueError as exc:
            raise urllib.error.URLError("invalid QR image redirect") from exc
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise urllib.error.URLError("QR image redirect is not HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@lru_cache(maxsize=32)
def _download_image(url: str) -> bytes:
    """Download a bounded official QR image, raising :class:`QrError` on failure."""

    if not _is_https_url(url):
        raise QrError("QR image URL must be an HTTPS URL")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/png,image/jpeg;q=0.9,*/*;q=0.1",
            "User-Agent": "unidl/qr",
        },
    )
    opener = urllib.request.build_opener(_HttpsRedirectHandler())
    last_error: BaseException | None = None
    for _attempt in range(MAX_REMOTE_IMAGE_ATTEMPTS):
        try:
            with opener.open(request, timeout=REMOTE_IMAGE_TIMEOUT) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_IMAGE_BYTES:
                    raise QrError("QR image is larger than the 4 MiB safety limit")
                raw = response.read(MAX_IMAGE_BYTES + 1)
            break
        except QrError:
            raise
        except (
            OSError,
            ValueError,
            http.client.HTTPException,
            urllib.error.URLError,
            urllib.error.HTTPError,
        ) as exc:
            last_error = exc
    else:
        raise QrError("official QR image could not be downloaded") from last_error
    if len(raw) > MAX_IMAGE_BYTES:
        raise QrError("QR image is larger than the 4 MiB safety limit")
    return raw


def _open_image(raw: bytes) -> Image.Image:
    from PIL import Image, UnidentifiedImageError

    try:
        opened = Image.open(io.BytesIO(raw))
        width, height = opened.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise QrError("QR image dimensions are outside the safety limit")
        opened.load()
        return opened.convert("RGB")
    except QrError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise QrError("QR image data is not a supported image") from exc


def _generated_image(
    payload: str,
    *,
    canvas: tuple[int, int] | None = None,
) -> Image.Image:
    """Encode a payload without ever drawing a non-square or partial module."""

    import qrcode

    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=QUIET_ZONE,
        box_size=1,
    )
    code.add_data(payload)
    code.make(fit=True)
    matrix = code.get_matrix()
    side = len(matrix)

    if canvas is None:
        scale = 8
        canvas_width = canvas_height = side * scale
    else:
        try:
            canvas_width, canvas_height = (int(canvas[0]), int(canvas[1]))
        except (IndexError, TypeError, ValueError) as exc:
            raise QrError("generated QR canvas dimensions are invalid") from exc
        if canvas_width <= 0 or canvas_height <= 0:
            raise QrError("generated QR canvas dimensions must be positive")
        if canvas_width * canvas_height > MAX_IMAGE_PIXELS:
            raise QrError("generated QR canvas is outside the safety limit")
        scale = min(canvas_width // side, canvas_height // side)
        if scale < 1:
            raise QrError("generated QR canvas is too small for its module grid")

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (canvas_width, canvas_height), "white")
    offset_x = (canvas_width - side * scale) // 2
    offset_y = (canvas_height - side * scale) // 2
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(matrix):
        top = offset_y + y * scale
        for x, dark in enumerate(row):
            if dark:
                left = offset_x + x * scale
                draw.rectangle(
                    (left, top, left + scale - 1, top + scale - 1),
                    fill="black",
                )
    return image


_FINDER = tuple(tuple(x in {0, 6} or y in {0, 6} or (2 <= x <= 4 and 2 <= y <= 4) for x in range(7)) for y in range(7))


def _sample(gray: Image.Image, side: int) -> Matrix:
    """Sample the centre of ``side`` square cells without antialiasing."""

    width, height = gray.size
    pixels = gray.load()
    return tuple(
        tuple(
            bool(
                pixels[min(width - 1, int((x + 0.5) * width / side)), min(height - 1, int((y + 0.5) * height / side))]
                < 160
            )
            for x in range(side)
        )
        for y in range(side)
    )


def _finder_score(grid: Matrix, modules: int, border: int) -> float:
    matches = 0
    total = 0
    for left, top in (
        (border, border),
        (border + modules - 7, border),
        (border, border + modules - 7),
    ):
        for y in range(7):
            for x in range(7):
                total += 1
                matches += grid[top + y][left + x] == _FINDER[y][x]

    # Timing patterns make the correct version/border win when two cell counts
    # happen to sample the large finder blocks reasonably well.
    for offset in range(8, max(8, modules - 8)):
        expected = offset % 2 == 0
        total += 2
        matches += grid[border + 6][border + offset] == expected
        matches += grid[border + offset][border + 6] == expected
    return matches / total if total else 0.0


def _pad(matrix: Matrix, border: int = QUIET_ZONE) -> Matrix:
    width = len(matrix)
    blank = (False,) * (width + border * 2)
    return (
        *((blank,) * border),
        *(tuple((False,) * border + row + (False,) * border) for row in matrix),
        *((blank,) * border),
    )


def _matrix_from_image(image: Image.Image) -> Matrix:
    """Recover a QR module grid from a square service-provided bitmap.

    QR versions have a known ``17 + 4*version`` module size.  Trying those sizes
    plus the usual quiet-zone widths and scoring all three finder patterns is
    considerably safer than treating every JPEG pixel as a terminal cell.  It
    also preserves a long QR exactly instead of downscaling two modules into one.
    """

    width, height = image.size
    if abs(width - height) > max(2, round(max(width, height) * 0.03)):
        raise QrError("QR image is not square enough to recover its module grid")

    from PIL import ImageOps

    gray = ImageOps.grayscale(image)
    samples: dict[int, Matrix] = {}
    best: tuple[float, int, int, Matrix] | None = None
    for version in range(1, 41):
        modules = 17 + version * 4
        for border in range(0, 9):
            side = modules + border * 2
            grid = samples.get(side)
            if grid is None:
                grid = samples[side] = _sample(gray, side)
            score = _finder_score(grid, modules, border)
            if best is None or score > best[0]:
                best = (score, modules, border, grid)

    if best is None or best[0] < 0.88:
        raise QrError("QR image module grid could not be recovered reliably")
    _, modules, border, grid = best
    core = tuple(tuple(row[border : border + modules]) for row in grid[border : border + modules])
    return _pad(core)


__all__ = ["Matrix", "QrError", "QrPresentation"]

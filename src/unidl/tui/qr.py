"""A QR widget with native terminal-image rendering and a text fallback."""

from __future__ import annotations

import asyncio
import os
import re
import select
import sys
import tempfile
import time
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

try:  # iTerm2 is macOS-only; keep importing the TUI on Windows.
    import termios
    import tty
except ImportError:  # pragma: no cover - platform-specific
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.geometry import Size
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Static

from ..core.qr import Matrix, QrError, QrPresentation

PROTOCOL_QR_WIDTH = 40
"""Width reserved by ``.qr-protocol-image`` in terminal cells."""


def _terminal_image_protocol(environ: Mapping[str, str] | None = None) -> str:
    """Choose a known protocol from environment only, without writing a probe.

    ``textual-image`` normally asks the terminal by emitting Kitty/Sixel control
    sequences during import.  Terminal.app and a number of embedded terminals
    print an unsupported Kitty query literally (``Gi=...;AAAA``), before Textual
    has even taken over the screen.  A QR has a lossless Unicode renderer, so an
    unknown terminal should use that instead of probing optimistically.

    ``UNIDL_TERMINAL_IMAGES=tgp|sixel|unicode`` is an escape hatch for terminals
    whose environment does not identify them.  It selects a renderer; it still
    never enables active capability queries.
    """

    values = environ if environ is not None else os.environ
    override = str(values.get("UNIDL_TERMINAL_IMAGES") or "auto").strip().lower()
    if override in {"off", "false", "0", "unicode", "text", "none"}:
        return ""
    if override in {"tgp", "kitty"}:
        return "tgp"
    if override == "sixel":
        return "sixel"
    if override != "auto":
        return ""

    term = str(values.get("TERM") or "").lower()
    term_program = str(values.get("TERM_PROGRAM") or "").lower()
    lc_terminal = str(values.get("LC_TERMINAL") or "").lower()
    if values.get("KITTY_WINDOW_ID") or "kitty" in term or term_program in {"kitty", "ghostty"}:
        return "tgp"
    if term_program in {"iterm.app", "wezterm"} or lc_terminal in {"iterm2", "wezterm"}:
        return "sixel"
    return ""


def _parse_iterm_cell_size(response: bytes | str) -> tuple[int, int] | None:
    """Parse an iTerm2 ReportCellSize response into physical pixels."""

    raw = response.encode() if isinstance(response, str) else response
    match = re.search(
        rb"\x1b\]1337;ReportCellSize=([0-9.]+);([0-9.]+)(?:;([0-9.]+))?",
        raw,
    )
    if not match:
        return None
    try:
        logical_height = float(match.group(1))
        logical_width = float(match.group(2))
        scale = float(match.group(3) or 1.0)
        width = max(1, round(logical_width * scale))
        height = max(1, round(logical_height * scale))
    except (TypeError, ValueError):
        return None
    return width, height


def _iterm_cell_size(environ: Mapping[str, str] | None = None) -> tuple[int, int] | None:
    """Ask iTerm2 for the physical pixel size of one terminal cell.

    ``TIOCGWINSZ`` reports logical points on a Retina display.  Sixel data is
    positioned in physical pixels, so using that value makes an image render at
    roughly half its intended size while Textual still reserves the full cell
    region.  iTerm2's private ``ReportCellSize`` response includes the Retina
    scale and is the only query we use here.  It runs before Textual starts its
    input reader; callers must not invoke it once the application is running.
    """

    values = environ if environ is not None else os.environ
    term_program = str(values.get("TERM_PROGRAM") or "").lower()
    lc_terminal = str(values.get("LC_TERMINAL") or "").lower()
    if term_program != "iterm.app" and lc_terminal != "iterm2":
        return None
    stdout = getattr(sys, "__stdout__", None)
    stdin = getattr(sys, "__stdin__", None)
    if termios is None or tty is None or stdout is None or stdin is None or not stdout.isatty() or not stdin.isatty():
        return None

    try:
        input_fd = stdin.buffer.fileno()
        original_mode = termios.tcgetattr(input_fd)
    except (AttributeError, OSError, termios.error):
        return None

    # iTerm2 accepts BEL-terminated OSC and answers with either a BEL or ST
    # terminated OSC.  Keep the read bounded: a non-iTerm intermediary must not
    # delay startup or consume ordinary input indefinitely.
    request = "\x1b]1337;ReportCellSize\x07"
    response = bytearray()
    try:
        tty.setcbreak(input_fd, termios.TCSANOW)
        stdout.write(request)
        stdout.flush()
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline and len(response) < 256:
            remaining = max(0.0, deadline - time.monotonic())
            readable, _writable, _exceptional = select.select([input_fd], [], [], remaining)
            if not readable:
                break
            chunk = os.read(input_fd, 1)
            if not chunk:
                break
            response.extend(chunk)
            if response.endswith(b"\x07") or response.endswith(b"\x1b\\"):
                break
    except (OSError, ValueError, termios.error):
        return None
    finally:
        try:
            termios.tcsetattr(input_fd, termios.TCSANOW, original_mode)
        except (OSError, ValueError, termios.error):
            pass

    return _parse_iterm_cell_size(response)


def _set_textual_image_cell_size(cell_size: tuple[int, int] | None) -> None:
    """Override textual-image's one-time cell-size cache when available."""

    if cell_size is None:
        return
    try:
        from textual_image._terminal import CellSize, get_cell_size

        width, height = cell_size
        get_cell_size._result = CellSize(width, height)  # type: ignore[attr-defined]
    except (ImportError, OSError):
        return


def protocol_cell_size() -> tuple[int, int] | None:
    """Read the already-primed native image cell size without probing the TTY."""

    try:
        from textual_image._terminal import get_cell_size

        cached = getattr(get_cell_size, "_result", None)
        width = int(getattr(cached, "width", 0) or 0)
        height = int(getattr(cached, "height", 0) or 0)
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def protocol_qr_canvas_size(
    cell_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Return the exact pixel rectangle occupied by the native QR widget."""

    cell_width, cell_height = cell_size or protocol_cell_size() or (10, 20)
    pixel_width = PROTOCOL_QR_WIDTH * cell_width
    cell_rows = max(1, round(pixel_width / cell_height))
    return pixel_width, cell_rows * cell_height


def _protocol_qr_image(image, canvas: tuple[int, int]):
    """Fit the original pixels to a cell canvas without stretching or blending."""
    from PIL import Image

    if image.size == canvas:
        return image
    width, height = canvas
    scale = min(width / image.width, height / image.height)
    fitted = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.NEAREST,
    )
    output = Image.new("RGB", canvas, "white")
    output.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return output


def _probe_winsize_cell_size() -> tuple[int, int]:
    """Read terminal cell pixels via TIOCGWINSZ without writing escape probes."""
    try:
        import fcntl
        import struct
        import termios as _termios

        for stream in (sys.__stdout__, sys.stdout):
            if stream is None or not hasattr(stream, "fileno"):
                continue
            try:
                if not stream.isatty():
                    continue
                packed = fcntl.ioctl(stream.fileno(), _termios.TIOCGWINSZ, b"\x00" * 8)
                rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
                if rows > 0 and cols > 0 and xpix > 0 and ypix > 0:
                    cw = max(1, round(xpix / cols))
                    ch = max(1, round(ypix / rows))
                    return (cw, ch)
            except (OSError, ValueError, _termios.error):
                continue
    except ImportError:
        pass
    return (10, 20)


def _prime_default_cell_size() -> None:
    """Ensure textual_image never runs live \\x1b[16t / termios probes inside Textual."""
    try:
        from textual_image._terminal import CellSize, get_cell_size

        if getattr(get_cell_size, "_result", None) is None:
            cw, ch = _probe_winsize_cell_size()
            get_cell_size._result = CellSize(cw, ch)  # type: ignore[attr-defined]
    except (ImportError, OSError):
        return


def prime_protocol_image() -> None:
    """Load the native image widget and prime terminal cell sizing before Textual."""

    protocol = _terminal_image_protocol()
    if not protocol:
        return
    # Importing textual-image fills its cache.  Set the queried value after the
    # import so it replaces the package's non-TTY fallback (10x20).
    if _load_protocol_image() is None:
        return
    if protocol == "sixel":
        _set_textual_image_cell_size(_iterm_cell_size())
    _prime_default_cell_size()


class _NoProbeOutput:
    """Forward stdout while making an import-time capability probe impossible."""

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped

    @staticmethod
    def isatty() -> bool:
        return False

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


@lru_cache(maxsize=1)
def _load_protocol_image():
    protocol = _terminal_image_protocol()
    if not protocol:
        return None

    # Importing any textual_image.widget submodule first executes that package's
    # __init__, whose automatic renderer probes stdout/stdin.  Make only that
    # import see a non-TTY stream, then choose the explicit class ourselves.
    # This preserves native rendering on known terminals without ever emitting a
    # query that an unsupported terminal can leak into the shell prompt.
    original_stdout = sys.__stdout__
    if original_stdout is None:
        return None
    sys.__stdout__ = _NoProbeOutput(original_stdout)  # type: ignore[assignment]
    try:  # pragma: no cover - native protocol selected by the user's terminal
        from textual_image.widget import SixelImage, TGPImage

        _prime_default_cell_size()
        return TGPImage if protocol == "tgp" else SixelImage
    except (ImportError, OSError):  # Python 3.11 or optional dependency absent
        return None
    finally:
        sys.__stdout__ = original_stdout


@lru_cache(maxsize=1)
def _load_halfcell_image():
    """Import the ANSI half-cell renderer without allowing capability probes."""

    original_stdout = sys.__stdout__
    if original_stdout is None:
        return None
    # Importing ``textual_image`` itself runs its automatic Sixel/TGP probes
    # when stdout looks like a TTY.  The half-cell fallback never needs those
    # probes, so make the package see a non-TTY stream during import as well.
    sys.__stdout__ = _NoProbeOutput(original_stdout)  # type: ignore[assignment]
    try:
        from textual_image.renderable.halfcell import Image as halfcell_image

        _prime_default_cell_size()
        return halfcell_image
    except (ImportError, OSError):  # pragma: no cover - optional dependency
        return None
    finally:
        sys.__stdout__ = original_stdout


def protocol_image_class():
    """Return the native image widget, importing it only when a QR is drawn."""

    return _load_protocol_image()


def halfcell_image_class():
    """Return the optional ANSI image renderer, loaded on first artwork use."""

    return _load_halfcell_image()


# Draw solid modules with the cell background. A font's full-block glyph can
# leave hairline gaps at the cell edges. The dedicated widget below preserves
# these backgrounds through Textual's rendering instead of styling Rich Text.
_INK = Style(color="#000000", bgcolor="#000000")
_PAPER = Style(bgcolor="#ffffff")
_INK_ON_PAPER = Style(color="#000000", bgcolor="#ffffff")
_PAPER_ON_INK = Style(color="#ffffff", bgcolor="#000000")


def half_block_qr(matrix: Matrix) -> Text:
    """Render one QR module per column and two per terminal row."""

    result = Text(no_wrap=True, overflow="crop")
    width = len(matrix[0]) if matrix else 0
    for top_index in range(0, len(matrix), 2):
        top = matrix[top_index]
        bottom = matrix[top_index + 1] if top_index + 1 < len(matrix) else (False,) * width
        for upper, lower in zip(top, bottom, strict=True):
            if upper and lower:
                result.append(" ", style=_INK)
            elif upper:
                result.append("▀", style=_INK_ON_PAPER)
            elif lower:
                result.append("▀", style=_PAPER_ON_INK)
            else:
                result.append(" ", style=_PAPER)
        if top_index + 2 < len(matrix):
            result.append("\n")
    return result


_BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


def braille_qr(matrix: Matrix) -> Text:
    """Preserve a dense QR in 2x4 modules per cell for narrow terminals."""

    result = Text(no_wrap=True, overflow="crop")
    height = len(matrix)
    width = len(matrix[0]) if matrix else 0
    for top in range(0, height, 4):
        for left in range(0, width, 2):
            bits = 0
            for y in range(4):
                for x in range(2):
                    if top + y < height and left + x < width and matrix[top + y][left + x]:
                        bits |= _BRAILLE_BITS[y][x]
            result.append(chr(0x2800 + bits) if bits else " ", style=_INK_ON_PAPER)
        if top + 4 < height:
            result.append("\n")
    return result


def unicode_qr(matrix: Matrix) -> Text:
    """Render a phone-scannable QR with one terminal column per module.

    Braille is compact but combines multiple modules into one character, which
    is useful for display but unreliable for camera scanning.
    """

    return half_block_qr(matrix)


class _QrModuleImage(Widget):
    """Paint QR cells directly, preserving per-cell backgrounds and exact size."""

    def __init__(self, matrix: Matrix) -> None:
        if not matrix or not matrix[0]:
            raise QrError("QR matrix is empty.")
        super().__init__(classes="qr-unicode-image")
        self._matrix = matrix
        self._width = len(matrix[0])
        self._height = (len(matrix) + 1) // 2
        self.styles.width = self._width
        self.styles.height = self._height

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return self._width

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return self._height

    def render_line(self, y: int) -> Strip:
        if not 0 <= y < self._height:
            return Strip.blank(self._width, _PAPER)
        top = self._matrix[y * 2]
        bottom = self._matrix[y * 2 + 1] if y * 2 + 1 < len(self._matrix) else (False,) * self._width
        segments = []
        for upper, lower in zip(top, bottom, strict=True):
            if upper == lower:
                segments.append(Segment(" ", _INK if upper else _PAPER))
            else:
                segments.append(Segment("▀", _INK_ON_PAPER if upper else _PAPER_ON_INK))
        return Strip(segments, cell_length=self._width)


class QrWidget(Vertical):
    """Display one QR without exposing its encoded/base64 value as text."""

    def __init__(self, presentation: QrPresentation) -> None:
        super().__init__(classes="qr-frame")
        self.presentation = presentation
        self._image = None
        self._preview_dir = None
        # Remote provider QR URLs can take up to 8s to download; load them
        # off the UI thread while keeping generated / in-memory QRs instant.
        self._remote_fetch = bool(
            not presentation.image_data and presentation._image_source_url()
        )

    def _build_widgets(self, image: object, matrix: Matrix | None, protocol_image: type | None) -> list[Widget]:
        from ..core.i18n import tr

        widgets: list[Widget] = []
        if protocol_image is not None:
            widgets.append(
                protocol_image(
                    _protocol_qr_image(image, protocol_qr_canvas_size()),
                    classes="qr-protocol-image",
                )
            )
        else:
            assert matrix is not None
            widgets.append(_QrModuleImage(matrix))
            warning = Static(tr("qr.too_wide"), classes="qr-render-error qr-size-error", markup=False)
            warning.display = False
            widgets.append(warning)
        widgets.append(Button(tr("qr.open_image"), classes="qr-open-image"))
        return widgets

    def _error_widget(self) -> Static:
        from ..core.i18n import tr

        # The link/action rows remain usable.  Do not include the exception:
        # it may contain a prefix of the data URI a service returned.
        message = tr("qr.no_image_link") if self.presentation.open_url else tr("qr.no_image")
        return Static(message, classes="qr-render-error", markup=False)

    def compose(self) -> ComposeResult:
        from ..core.i18n import tr

        if self._remote_fetch:
            yield Static(tr("qr.loading", default="Loading QR code…"), classes="qr-loading", markup=False)
            return
        try:
            protocol_image = protocol_image_class()
            self._image = self.presentation.image(
                generated_canvas=protocol_qr_canvas_size() if protocol_image is not None else None,
            )
            matrix = None if protocol_image is not None else self.presentation.matrix(image=self._image)
            yield from self._build_widgets(self._image, matrix, protocol_image)
        except (QrError, ImportError, OSError, ValueError, RuntimeError):
            self._image = None
            yield self._error_widget()

    def _fit_parent_card(self) -> None:
        modules = self.query(".qr-unicode-image")
        if modules and self.parent is not None and self.parent.has_class("qr-card"):
            # Let a wide terminal show the whole grid, while the card's CSS
            # max-width still limits it in small terminals.
            self.parent.styles.width = max(
                78, modules.first()._width + self.parent.styles.gutter.width,
            )
        self.on_resize()

    def on_mount(self) -> None:
        if self._remote_fetch:
            self._load_remote_qr()
            return
        self._fit_parent_card()

    @work(exclusive=True, group="qr-fetch")
    async def _load_remote_qr(self) -> None:
        def fetch():
            protocol_image = protocol_image_class()
            image = self.presentation.image(
                generated_canvas=protocol_qr_canvas_size() if protocol_image is not None else None,
            )
            matrix = None if protocol_image is not None else self.presentation.matrix(image=image)
            return image, matrix, protocol_image

        try:
            image, matrix, protocol_image = await asyncio.to_thread(fetch)
        except (QrError, ImportError, OSError, ValueError, RuntimeError):
            if not self.is_attached:
                return
            await self.remove_children()
            await self.mount(self._error_widget())
            return
        if not self.is_attached:
            return
        try:
            self._image = image
            widgets = self._build_widgets(image, matrix, protocol_image)
        except (QrError, ImportError, OSError, ValueError, RuntimeError):
            self._image = None
            await self.remove_children()
            await self.mount(self._error_widget())
            return
        await self.remove_children()
        await self.mount_all(widgets)
        self._fit_parent_card()

    def on_resize(self) -> None:
        modules = self.query(".qr-unicode-image")
        warning = self.query(".qr-size-error")
        if not modules or not warning:
            return
        child = modules.first()
        fits = self.content_size.width >= child._width
        child.display = fits
        warning.first().display = not fits

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not event.button.has_class("qr-open-image") or self._image is None:
            return
        event.stop()
        try:
            if self._preview_dir is None:
                self._preview_dir = tempfile.TemporaryDirectory(prefix="unidl-qr-")
            path = Path(self._preview_dir.name) / "qr.png"
            self._image.save(path, format="PNG")
            self.app.open_path(path)
        except OSError as exc:
            self.notify(str(exc), severity="error", timeout=6)

    def on_unmount(self) -> None:
        """Release a Kitty terminal-side image as soon as the wait ends."""
        self.workers.cancel_group(self, "qr-fetch")
        found = self.query(".qr-protocol-image")
        child = found.first() if found else None
        renderable = getattr(child, "_renderable", None)
        cleanup = getattr(renderable, "cleanup", None)
        if callable(cleanup):
            cleanup()
            child._renderable = None
        if self._preview_dir is not None:
            self._preview_dir.cleanup()
            self._preview_dir = None
        self._image = None


__all__ = [
    "QrWidget",
    "braille_qr",
    "half_block_qr",
    "unicode_qr",
    "protocol_image_class",
    "protocol_cell_size",
    "protocol_qr_canvas_size",
    "halfcell_image_class",
]

"""Open-source acknowledgements shown from the Home screen caption."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..core.i18n import tr
from .chrome import CloseMark

OPEN_SOURCE_PROJECTS = (
    ("Python", "runtime", "PSF-2.0", "https://www.python.org/"),
    ("Textual", "terminal user interface", "MIT", "https://github.com/Textualize/textual"),
    ("Requests", "HTTP client", "Apache-2.0", "https://github.com/psf/requests"),
    ("cryptography", "cryptographic primitives", "Apache-2.0 / BSD-3-Clause", "https://github.com/pyca/cryptography"),
    ("pywidevine", "Widevine CDM protocol", "GPL-3.0-only", "https://github.com/devine-dl/pywidevine"),
    ("pyplayready", "PlayReady CDM protocol", "CC BY-NC-ND 4.0", "https://git.gay/ready-dl/pyplayready"),
    ("httpx", "optional HTTP/2 client support", "BSD-3-Clause", "https://github.com/encode/httpx"),
    ("PyYAML", "configuration parsing", "MIT", "https://github.com/yaml/pyyaml"),
    ("PyCryptodome", "cryptographic primitives", "BSD / Public Domain", "https://github.com/Legrandin/pycryptodome"),
    ("Wasmtime", "WebAssembly runtime", "Apache-2.0", "https://github.com/bytecodealliance/wasmtime-py"),
    ("PyArabic / arabic-reshaper", "right-to-left text shaping", "LGPL-3.0 / MIT", "https://github.com/mpcabd/python-arabic-reshaper"),
    ("python-bidi", "bidirectional text layout", "LGPL-3.0", "https://github.com/MeirKriheli/python-bidi"),
    ("qrcode", "terminal QR support", "BSD", "https://github.com/lincolnloop/python-qrcode"),
    ("geonamescache", "country and region names", "MIT", "https://github.com/yaph/geonamescache"),
    ("FFmpeg", "media conversion and muxing", "LGPL/GPL build dependent", "https://ffmpeg.org/"),
    (
        "N_m3u8DL-RE",
        "cross-platform DASH/HLS/MSS downloader for VOD and live media",
        "MIT",
        "https://github.com/nilaoda/N_m3u8DL-RE",
    ),
    (
        "unshackle",
        "service architecture and media delivery reference",
        "GPL-3.0-only",
        "https://github.com/unshackle-dl/unshackle",
    ),
    (
        "pydecrypt",
        "MP4 CENC/CBCS and WebM media decryption",
        "GPL-3.0-only",
        "https://github.com/Hugoved/pydecrypt",
    ),
)


class AboutScreen(ModalScreen[None]):
    """Show attribution without leaving the Home screen."""

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Close", show=False),
        Binding("x", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Close", show=False),
        Binding("b", "close", "Close", show=False),
        Binding("up", "scroll(-2)", "Up", show=False),
        Binding("down", "scroll(2)", "Down", show=False),
        Binding("pageup", "scroll(-12)", "Page up", show=False),
        Binding("pagedown", "scroll(12)", "Page down", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="about-modal"):
            with Horizontal(id="about-modal-head"):
                yield Static(tr("about.title"), id="about-modal-title")
                yield CloseMark(id="about-modal-close")
            yield Static(tr("about.intro"), id="about-intro")
            with VerticalScroll(id="about-list"):
                for name, purpose, license_name, url in OPEN_SOURCE_PROJECTS:
                    yield Static(
                        f"[$foreground]{name}[/]  [$muted]— {purpose}[/]\n"
                        f"[$dim]{license_name} · {url}[/]",
                        classes="about-row",
                    )
            yield Static(tr("about.license_note"), id="about-license-note")
            yield Static(tr("about.close_hint"), id="about-modal-hint")

    def on_mount(self) -> None:
        self.query_one("#about-list", VerticalScroll).focus()

    def action_scroll(self, delta: int) -> None:
        self.query_one("#about-list", VerticalScroll).scroll_relative(y=delta, animate=False)

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["AboutScreen", "OPEN_SOURCE_PROJECTS"]

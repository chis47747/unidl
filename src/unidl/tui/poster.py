"""Reusable 2:3 movie/series poster preview rail with terminal compatibility.

Designed specifically for list/detail screens (Scheduled Downloads, Upcoming
Radar, JustWatch search & availability):
* Three global modes via ``poster_preview``: ``auto`` (default), ``always``, ``off``.
* ``auto`` enables native crisp posters ONLY when the terminal supports Kitty
  Graphics Protocol (TGP) or Sixel AND viewport size fits cleanly (>= 104 cols).
  macOS native ``Terminal.app`` (``Apple_Terminal``), basic terminals, and
  narrow panes collapse automatically to full-width text with zero HTTP fetches.
* ``always`` allows opt-in text-cell rendering: truecolor terminals use half-cell
  blocks, while macOS ``Terminal.app`` uses solid coloured spaces (avoiding
  half-block horizontal hairline gaps).
* 150 ms highlight debounce + bounded L1 RAM LRU & L2 private disk cache.
* Four-point native graphics cleanup (unmount, screen suspend/resume, hide/tab
  switch, theme repaint) so Kitty/Sixel layers never leave ghost artifacts.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Static

from ..core.i18n import cell_width, tr
from ..core.secureio import FILE_MODE, atomic_write_bytes, private_directory
from .audio import (
    _contain_dimensions,
    _cover_bytes,
    _cover_pixels,
    _cover_renderable,
    _hex_rgb,
    _letterbox_image,
    _open_cover,
)
from .bidi import is_apple_terminal, visual_text
from .qr import _terminal_image_protocol, protocol_cell_size, protocol_image_class

if TYPE_CHECKING:
    from PIL import Image

POSTER_PREVIEW_OPTIONS: tuple[str, ...] = ("auto", "always", "off")
DEFAULT_POSTER_PREVIEW = "auto"

POSTER_MIN_SCREEN_WIDTH = 104
POSTER_MIN_SCREEN_HEIGHT = 20
POSTER_RAIL_WIDTH = 44
POSTER_MAX_WIDTH = 30
POSTER_MAX_HEIGHT = 18
POSTER_MIN_WIDTH = 12
POSTER_MIN_HEIGHT = 9
POSTER_DEBOUNCE_SECONDS = 0.15

POSTER_RAM_CACHE_LIMIT = 32
POSTER_DISK_CACHE_LIMIT = 200
POSTER_DISK_CACHE_MAX_BYTES = 25 * 1024 * 1024
POSTER_DISK_TTL_SECONDS = 7 * 24 * 3600


def normalize_poster_preview(value: object) -> str:
    key = str(value or "").strip().lower()
    return key if key in POSTER_PREVIEW_OPTIONS else DEFAULT_POSTER_PREVIEW


def detect_terminal_poster_capability(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(capability_kind, terminal_label)`` without writing any probe.

    ``capability_kind`` is one of:
    ``"tgp"`` | ``"sixel"`` | ``"apple_terminal"`` | ``"truecolor"`` | ``"basic"`` | ``"dumb"``.
    """
    values = environ if environ is not None else os.environ
    term = str(values.get("TERM") or "").strip().lower()
    if term == "dumb":
        return "dumb", "dumb"

    proto = _terminal_image_protocol(values)
    term_program = str(values.get("TERM_PROGRAM") or "").strip()
    lc_terminal = str(values.get("LC_TERMINAL") or "").strip()

    if proto == "tgp":
        label = term_program or ("Kitty" if values.get("KITTY_WINDOW_ID") or "kitty" in term else "Kitty/TGP")
        return "tgp", label
    if proto == "sixel":
        label = term_program or lc_terminal or "Sixel"
        return "sixel", label
    is_apple = (
        term_program.lower() == "apple_terminal"
        if environ is not None
        else is_apple_terminal()
    )
    if is_apple:
        return "apple_terminal", "macOS Terminal.app"

    colorterm = str(values.get("COLORTERM") or "").strip().lower()
    if colorterm in {"truecolor", "24bit"}:
        label = term_program or term or "TrueColor terminal"
        return "truecolor", label
    return "basic", term_program or term or "terminal"


def should_show_poster(
    mode: str,
    *,
    width: int,
    height: int,
    environ: Mapping[str, str] | None = None,
    protocol_available: bool | None = None,
) -> tuple[bool, str, str]:
    """Decide whether and how to render the poster rail at the given viewport.

    Returns ``(visible, renderer, reason)`` where:
    * ``renderer`` is ``"protocol"`` | ``"halfcell"`` | ``"pixels"`` | ``"none"``.
    * ``reason`` is ``"native"`` | ``"text_fallback"`` | ``"apple_terminal_pixels"``
      | ``"off"`` | ``"dumb"`` | ``"narrow"`` | ``"no_protocol"``.
    """
    normalized = normalize_poster_preview(mode)
    if normalized == "off":
        return False, "none", "off"

    cap, _label = detect_terminal_poster_capability(environ)
    if cap == "dumb":
        return False, "none", "dumb"

    if width < POSTER_MIN_SCREEN_WIDTH or height < POSTER_MIN_SCREEN_HEIGHT:
        return False, "none", "narrow"

    has_protocol = (
        bool(protocol_available)
        if protocol_available is not None
        else (cap in {"tgp", "sixel"} and protocol_image_class() is not None)
    )

    if normalized == "auto":
        if has_protocol:
            return True, "protocol", "native"
        return False, "none", "no_protocol"

    # mode == "always"
    if has_protocol:
        return True, "protocol", "native"
    if cap == "apple_terminal":
        return True, "pixels", "apple_terminal_pixels"
    return True, "halfcell", "text_fallback"


def poster_capability_summary(
    environ: Mapping[str, str] | None = None,
    *,
    protocol_available: bool | None = None,
) -> str:
    """Short localized summary of detected terminal poster support for Settings."""
    cap, label = detect_terminal_poster_capability(environ)
    has_protocol = (
        bool(protocol_available)
        if protocol_available is not None
        else (cap in {"tgp", "sixel"} and protocol_image_class() is not None)
    )
    if has_protocol:
        proto = "Kitty/TGP" if cap == "tgp" else "Sixel"
        return tr(
            "poster.cap.native",
            terminal=label,
            protocol=proto,
            default=f"Current terminal: {label} ({proto} native images — Auto shows posters)",
        )
    if cap == "apple_terminal":
        return tr(
            "poster.cap.apple_terminal",
            default="Current terminal: macOS Terminal.app (no native image protocol — Auto hides posters; choose Always for solid-cell preview)",
        )
    if cap == "dumb":
        return tr(
            "poster.cap.dumb",
            default="Current terminal: dumb (posters disabled)",
        )
    return tr(
        "poster.cap.fallback",
        terminal=label,
        default=f"Current terminal: {label} (no native image protocol — Auto hides posters; choose Always for text-cell preview)",
    )


def poster_dimensions(
    image: Image.Image | None,
    max_width: int,
    max_height: int,
) -> tuple[int, int]:
    """Compute portrait 2:3 terminal cell dimensions inside ``max_width x max_height``."""
    mw = max(POSTER_MIN_WIDTH, min(POSTER_MAX_WIDTH, int(max_width)))
    mh = max(POSTER_MIN_HEIGHT, min(POSTER_MAX_HEIGHT, int(max_height)))
    if image is not None:
        w, h = _contain_dimensions(image, mw, mh)
    else:
        cell_w, cell_h = protocol_cell_size() or (1, 2)
        ratio = 2.0 / 3.0
        w = mw
        h = max(POSTER_MIN_HEIGHT, round(w * cell_w / (ratio * cell_h)))
        if h > mh:
            h = mh
            w = max(POSTER_MIN_WIDTH, round(h * ratio * cell_h / cell_w))
    w = max(POSTER_MIN_WIDTH, w - (w % 2))
    h = max(POSTER_MIN_HEIGHT, min(mh, h))
    return min(w, mw), h


# --------------------------------------------------------------- bounded cache


class PosterCache:
    """Two-tier bounded cache: in-memory decoded LRU + private disk thumbnails."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._ram: OrderedDict[str, Image.Image] = OrderedDict()
        self._disk_dir = Path(cache_dir) if cache_dir is not None else None

    def set_cache_dir(self, cache_dir: Path | None) -> None:
        with self._lock:
            self._disk_dir = Path(cache_dir) if cache_dir is not None else None

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]

    def get_ram(self, url: str) -> Image.Image | None:
        url = str(url or "").strip()
        if not url:
            return None
        with self._lock:
            img = self._ram.get(url)
            if img is not None:
                self._ram.move_to_end(url)
            return img

    def put_ram(self, url: str, image: Image.Image) -> None:
        with self._lock:
            self._ram[url] = image
            self._ram.move_to_end(url)
            while len(self._ram) > POSTER_RAM_CACHE_LIMIT:
                self._ram.popitem(last=False)

    def _disk_path(self, url: str) -> Path | None:
        with self._lock:
            base = self._disk_dir
        if base is None:
            return None
        return base / f"{self._key(url)}.img"

    def get_disk_bytes(self, url: str) -> bytes | None:
        path = self._disk_path(url)
        if path is None or not path.is_file():
            return None
        try:
            stat = path.stat()
            if (time.time() - stat.st_mtime) > POSTER_DISK_TTL_SECONDS:
                path.unlink(missing_ok=True)
                return None
            return path.read_bytes()
        except OSError:
            return None

    def put_disk_bytes(self, url: str, data: bytes) -> None:
        path = self._disk_path(url)
        if path is None or not data:
            return
        try:
            private_directory(path.parent)
            atomic_write_bytes(path, data, mode=FILE_MODE)
            self._prune_disk(path.parent)
        except OSError:
            return

    def _prune_disk(self, directory: Path) -> None:
        try:
            entries = [
                (p.stat().st_mtime, p.stat().st_size, p)
                for p in directory.glob("*.img")
                if p.is_file()
            ]
        except OSError:
            return
        now = time.time()
        kept: list[tuple[float, int, Path]] = []
        total_bytes = 0
        for mtime, size, path in sorted(entries, key=lambda item: item[0], reverse=True):
            if (now - mtime) > POSTER_DISK_TTL_SECONDS:
                path.unlink(missing_ok=True)
                continue
            if len(kept) >= POSTER_DISK_CACHE_LIMIT or (total_bytes + size) > POSTER_DISK_CACHE_MAX_BYTES:
                path.unlink(missing_ok=True)
                continue
            kept.append((mtime, size, path))
            total_bytes += size

    def fetch(self, url: str, *, proxy: str | None = None) -> Image.Image:
        """Load one HTTP(S) poster thumbnail through RAM -> disk -> network."""
        clean = str(url or "").strip()
        parsed = urlsplit(clean)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("poster URL must be HTTP or HTTPS")

        cached = self.get_ram(clean)
        if cached is not None:
            return cached

        raw = self.get_disk_bytes(clean)
        if raw is not None:
            try:
                image = _open_cover(raw)
                self.put_ram(clean, image)
                return image
            except ValueError:
                pass

        # Public poster fetch only: never forward service Cookie/Authorization headers.
        raw = _cover_bytes(clean, {"User-Agent": "Mozilla/5.0"}, proxy)
        image = _open_cover(raw)
        self.put_ram(clean, image)
        self.put_disk_bytes(clean, raw)
        return image


_SHARED_CACHE = PosterCache()


def shared_poster_cache(app: Any = None) -> PosterCache:
    config = getattr(app, "config", None)
    paths = getattr(config, "paths", None)
    cache_root = getattr(paths, "cache", None)
    if cache_root is not None:
        _SHARED_CACHE.set_cache_dir(Path(cache_root) / "posters")
    return _SHARED_CACHE


# --------------------------------------------------------------- preview widget


@dataclass(frozen=True)
class PosterItem:
    title: str
    subtitle: str = ""
    poster_url: str = ""
    meta_lines: tuple[tuple[str, str], ...] = ()
    synopsis: str = ""
    footer_hint: str = ""


def _clear_native_widget(widget: Any) -> None:
    if widget is None or isinstance(widget, Static):
        return
    try:
        renderable = getattr(widget, "_renderable", None)
        cleanup = getattr(renderable, "cleanup", None)
        if callable(cleanup):
            cleanup()
            widget._renderable = None
    except Exception:
        pass
    try:
        widget.image = None
    except Exception:
        pass


class PosterPreviewWidget(Vertical):
    """Fixed-width side rail rendering a 2:3 poster + structured metadata card."""

    def __init__(self, *, id: str = "poster-rail", classes: str = "poster-rail") -> None:
        super().__init__(id=id, classes=classes)
        self._item: PosterItem | None = None
        self._proxy: str | None = None
        self._renderer: str = "protocol"
        self._raw_image: Image.Image | None = None
        self._load_token = 0
        self._debounce_timer: Timer | None = None
        self._suspended = False
        self._box_size: tuple[int, int] = (POSTER_MAX_WIDTH, POSTER_MAX_HEIGHT)

    def compose(self) -> ComposeResult:
        proto_cls = protocol_image_class()
        with Vertical(id=f"{self.id}-art-wrap", classes="poster-art-wrap"):
            yield Static("", id=f"{self.id}-status", classes="poster-status")
            if proto_cls is not None:
                img = proto_cls(id=f"{self.id}-native", classes="poster-native")
                img.display = False
                yield img
            yield Static("", id=f"{self.id}-fallback", classes="poster-fallback")
        with VerticalScroll(id=f"{self.id}-meta-scroll", classes="poster-meta-scroll"):
            yield Static("", id=f"{self.id}-meta", classes="poster-meta")

    def on_mount(self) -> None:
        signal = getattr(self.app, "theme_changed_signal", None)
        if signal is not None:
            signal.subscribe(self, self._on_theme_changed)
        fallback = self._fallback_widget()
        if fallback is not None:
            fallback.display = False
        self._apply_box_layout()
        if self._item is not None:
            self._render_meta()

    def on_resize(self) -> None:
        self._apply_box_layout()
        if self._raw_image is not None and not self._suspended and self.display:
            self._paint_image(self._raw_image)

    def on_unmount(self) -> None:
        self._cancel_debounce()
        self._load_token += 1
        signal = getattr(self.app, "theme_changed_signal", None)
        if signal is not None:
            signal.unsubscribe(self)
        self.clear_graphics()
        self._raw_image = None

    def _on_theme_changed(self, _theme: object = None) -> None:
        if not self.is_attached or not self.is_mounted:
            return
        self._render_meta()
        if self._raw_image is not None and not self._suspended and self.display:
            self._paint_image(self._raw_image)

    def _status_widget(self) -> Static | None:
        found = self.query(f"#{self.id}-status")
        return found.first(Static) if found else None

    def _native_widget(self) -> Any:
        found = self.query(f"#{self.id}-native")
        return found.first() if found else None

    def _fallback_widget(self) -> Static | None:
        found = self.query(f"#{self.id}-fallback")
        return found.first(Static) if found else None

    def _meta_widget(self) -> Static | None:
        found = self.query(f"#{self.id}-meta")
        return found.first(Static) if found else None

    def _art_wrap(self) -> Vertical | None:
        found = self.query(f"#{self.id}-art-wrap")
        return found.first(Vertical) if found else None

    def _cancel_debounce(self) -> None:
        if self._debounce_timer is not None:
            try:
                self._debounce_timer.stop()
            except Exception:
                pass
            self._debounce_timer = None

    def clear_graphics(self) -> None:
        """Release any native Kitty/Sixel image layer to prevent ghost artifacts."""
        self._cancel_debounce()
        self._load_token += 1
        native = self._native_widget()
        if native is not None:
            _clear_native_widget(native)
            native.display = False
        fallback = self._fallback_widget()
        if fallback is not None:
            fallback.update("")
            fallback.display = False

    def suspend_graphics(self) -> None:
        """Call from host screen ``on_screen_suspend`` before pushing a modal."""
        self._suspended = True
        self._cancel_debounce()
        self.clear_graphics()

    def resume_graphics(self) -> None:
        """Call from host screen ``on_screen_resume`` to repaint cached poster."""
        self._suspended = False
        if not self.is_mounted or not self.display:
            return
        if self._raw_image is not None:
            self._paint_image(self._raw_image)
        elif self._item is not None and self._item.poster_url:
            self._schedule_load(self._item.poster_url)

    def _sync_rail_width_class(self) -> None:
        """Adapt inspector width smoothly across medium, standard, and ultrawide terminals."""
        viewport_w = int(getattr(self.screen, "size", None).width or getattr(self.app, "size", None).width or 120)
        self.set_class(viewport_w < 126, "compact-rail")
        self.set_class(viewport_w >= 156, "wide-rail")

    def _apply_box_layout(self) -> None:
        self._sync_rail_width_class()
        height = int(self.size.height or 24)
        width = max(POSTER_MIN_WIDTH, int(self.size.width or POSTER_RAIL_WIDTH) - 6)
        # Keep vertical room for title, structured details, and synopsis below the poster,
        # while still showing a compact poster on standard 24- and 28-row terminals.
        reserve = 6 if height < 18 else 11
        min_art = 6 if height < 18 else POSTER_MIN_HEIGHT
        max_art_height = max(min_art, min(POSTER_MAX_HEIGHT, height - reserve))
        box_w, box_h = poster_dimensions(self._raw_image, min(POSTER_MAX_WIDTH, width), max_art_height)
        self._box_size = (box_w, box_h)
        show_art_box = bool(self._item and self._item.poster_url) and height >= 11
        wrap = self._art_wrap()
        if wrap is not None:
            wrap.display = show_art_box
            wrap.styles.height = box_h if show_art_box else 0
        for widget in (self._status_widget(), self._native_widget(), self._fallback_widget()):
            if widget is not None:
                widget.styles.width = box_w
                widget.styles.height = box_h

    def set_item(
        self,
        item: PosterItem | None,
        *,
        renderer: str = "protocol",
        proxy: str | None = None,
    ) -> None:
        """Update displayed metadata and debounced poster fetch."""
        self._cancel_debounce()
        self._renderer = renderer
        self._proxy = proxy
        same_url = (
            item is not None
            and self._item is not None
            and item.poster_url
            and item.poster_url == self._item.poster_url
            and self._raw_image is not None
        )
        self._item = item
        if not self.is_mounted:
            return
        self._render_meta()

        if item is None or not item.poster_url or renderer == "none":
            self._raw_image = None
            self._apply_box_layout()
            self.clear_graphics()
            status = self._status_widget()
            if status is not None:
                status.update("")
                status.display = False
            return

        if same_url:
            self._apply_box_layout()
            wrap = self._art_wrap()
            if not self._suspended and (wrap is None or wrap.display):
                self._paint_image(self._raw_image)
            return

        cache = shared_poster_cache(self.app)
        cached = cache.get_ram(item.poster_url)
        if cached is not None:
            self._load_token += 1
            self._raw_image = cached
            self._apply_box_layout()
            wrap = self._art_wrap()
            if not self._suspended and (wrap is None or wrap.display):
                self._paint_image(cached)
            return

        self._raw_image = None
        self._apply_box_layout()
        self.clear_graphics()
        wrap = self._art_wrap()
        if wrap is not None and not wrap.display:
            return
        status = self._status_widget()
        if status is not None:
            status.update(tr("poster.loading", default="Loading poster…"))
            status.display = True
        if not self._suspended:
            self._schedule_load(item.poster_url)

    def update_meta(self, item: PosterItem | None) -> None:
        """Refresh only the detail metadata text without repainting cached poster graphics."""
        if not self.is_mounted:
            self._item = item
            return
        if (
            item is not None
            and self._item is not None
            and item.poster_url == self._item.poster_url
        ):
            self._item = item
            self._render_meta()
            return
        self.set_item(item, renderer=self._renderer, proxy=self._proxy)

    def _schedule_load(self, url: str) -> None:
        self._cancel_debounce()
        self._load_token += 1
        token = self._load_token
        self._debounce_timer = self.set_timer(
            POSTER_DEBOUNCE_SECONDS,
            lambda: self._load_poster_worker(token, url, self._proxy),
        )

    @work(thread=True, exclusive=True, group="poster-fetch")
    def _load_poster_worker(self, token: int, url: str, proxy: str | None) -> None:
        cache = shared_poster_cache(self.app)
        try:
            image = cache.fetch(url, proxy=proxy)
        except Exception:
            try:
                self.app.call_from_thread(self._finish_load, token, None)
            except RuntimeError:
                pass
            return
        try:
            self.app.call_from_thread(self._finish_load, token, image)
        except RuntimeError:
            pass

    def _finish_load(self, token: int, image: Image.Image | None) -> None:
        if token != self._load_token or not self.is_mounted:
            return
        self._raw_image = image
        if image is None:
            self.clear_graphics()
            status = self._status_widget()
            if status is not None:
                status.update(tr("poster.unavailable", default="Poster unavailable"))
                status.display = True
            return
        self._apply_box_layout()
        wrap = self._art_wrap()
        if not self._suspended and self.display and (wrap is None or wrap.display):
            self._paint_image(image)

    def _paint_image(self, image: Image.Image) -> None:
        status = self._status_widget()
        native = self._native_widget()
        fallback = self._fallback_widget()
        box_w, box_h = self._box_size
        if status is not None:
            status.display = False

        if self._renderer == "protocol" and native is not None:
            if fallback is not None:
                fallback.display = False
            palette = getattr(self.app, "palette", None)
            bg = _hex_rgb(getattr(palette, "panel", None) or getattr(palette, "bg", None))
            native.styles.width = box_w
            native.styles.height = box_h
            native.display = True
            native.image = _letterbox_image(image, box_w, box_h, background=bg)
            return

        if native is not None:
            _clear_native_widget(native)
            native.display = False
        if fallback is not None:
            w, h = poster_dimensions(image, box_w, box_h)
            if self._renderer == "pixels":
                renderable = _cover_pixels(image, width=w, height=h)
            else:
                renderable = _cover_renderable(image, width=w, height=h)
            fallback.styles.width = w
            fallback.styles.height = h
            fallback.display = True
            fallback.update(renderable, layout=False)

    def _render_meta(self) -> None:
        meta = self._meta_widget()
        if meta is None:
            return
        item = self._item
        if item is None:
            meta.update("")
            return
        palette = getattr(self.app, "palette", None)
        fg = getattr(palette, "fg2", "white")
        fg_body = getattr(palette, "fg", fg)
        accent = getattr(palette, "accent", "cyan")
        muted = getattr(palette, "muted", "bright_black")
        dim = getattr(palette, "dim", "bright_black")
        rule_color = getattr(palette, "rule", None) or getattr(palette, "gutter", dim)
        ok = getattr(palette, "ok", "green")
        warn = getattr(palette, "warn", "yellow")
        bad = getattr(palette, "error", None) or getattr(palette, "bad", "red")
        role_color = {
            "accent": accent,
            "ok": ok,
            "warn": warn,
            "bad": bad,
            "muted": muted,
            "dim": dim,
            "fg": fg,
        }

        inner_w = max(24, int(self.size.width or POSTER_RAIL_WIDTH) - 6)
        rule_line = "─" * min(inner_w, 42)

        out = Text(no_wrap=False, overflow="fold")
        out.append(visual_text(item.title), style=f"bold {fg}")
        if item.subtitle:
            out.append(f"\n{visual_text(item.subtitle)}", style=accent)

        status_rows: list[tuple[str, str]] = []
        kv_rows: list[tuple[str, str]] = []
        for label, value in item.meta_lines:
            if not value and not label:
                continue
            if label in role_color and value:
                status_rows.append((label, value))
            else:
                kv_rows.append((label, value))

        if status_rows or kv_rows:
            out.append(f"\n{rule_line}", style=rule_color)

        for role, value in status_rows:
            out.append(f"\n{visual_text(value)}", style=f"bold {role_color[role]}" if role in {"ok", "warn", "bad"} else role_color[role])

        if kv_rows:
            max_lbl = min(
                12,
                max((cell_width(lbl) for lbl, val in kv_rows if lbl and val), default=0),
            )
            for label, value in kv_rows:
                out.append("\n")
                if label and value:
                    pad = " " * max(1, (max_lbl - cell_width(label)) + 2)
                    out.append(f"{visual_text(label)}{pad}", style=muted)
                    out.append(visual_text(value), style=fg_body)
                else:
                    out.append(visual_text(label or value), style=muted)

        if item.synopsis:
            snippet = " ".join(item.synopsis.strip().split())
            if len(snippet) > 420:
                snippet = snippet[:417].rstrip() + "…"
            overview_label = tr("poster.section.overview", default="Overview")
            out.append(f"\n\n{rule_line}", style=rule_color)
            out.append(f"\n{visual_text(overview_label)}", style=f"bold {muted}")
            out.append(f"\n{visual_text(snippet)}", style=dim)

        if item.footer_hint:
            out.append(f"\n\n▸ {visual_text(item.footer_hint)}", style=f"bold {accent}")
        meta.update(out)


__all__ = [
    "DEFAULT_POSTER_PREVIEW",
    "POSTER_DEBOUNCE_SECONDS",
    "POSTER_MIN_SCREEN_HEIGHT",
    "POSTER_MIN_SCREEN_WIDTH",
    "POSTER_PREVIEW_OPTIONS",
    "POSTER_RAIL_WIDTH",
    "PosterCache",
    "PosterItem",
    "PosterPreviewWidget",
    "detect_terminal_poster_capability",
    "normalize_poster_preview",
    "poster_capability_summary",
    "poster_dimensions",
    "shared_poster_cache",
    "should_show_poster",
]

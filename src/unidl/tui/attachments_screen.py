"""Modal viewer for service-provided posters, thumbnails and artwork."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..core.attachments import Attachment, count_label
from ..core.i18n import tr
from .audio import (
    COVER_WIDTH,
    _contain_dimensions,
    _contain_image,
    _cover_bytes,
    _cover_renderable,
    _hex_rgb,
    _letterbox_image,
    _open_cover,
    protocol_image_class,
)
from .bidi import visual_markup
from .chrome import CloseMark

if TYPE_CHECKING:
    from PIL import Image


class AttachmentsScreen(ModalScreen[None]):
    """Show one attachment at a time; arrows move through a multi-image set."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("x", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Back", show=False),
        Binding("b", "close", "Back", show=False),
        Binding("left", "previous", "Previous", show=False),
        Binding("right", "next", "Next", show=False),
        Binding("up", "previous", "Previous", show=False),
        Binding("down", "next", "Next", show=False),
    ]

    def __init__(self, save_name: str, attachments: Sequence[Attachment], *, proxy: str | None = None) -> None:
        super().__init__()
        self.save_name = str(save_name)
        self.attachments = tuple(attachments)
        self.proxy = proxy
        self.index = 0
        self._load_token = 0
        self._raw_image: Image.Image | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="attachment-modal"):
            with Horizontal(id="attachment-modal-head"):
                yield Static(
                    f"{tr('attachments.title')}  [$dim]{visual_markup(self.save_name)}[/]",
                    id="attachment-modal-title",
                )
                yield CloseMark(id="attachment-modal-close")
            yield Static("", id="attachment-summary")
            protocol_image = protocol_image_class()
            if protocol_image is not None:
                yield Static(tr("attachments.loading"), id="attachment-status", classes="attachment-image")
                img = protocol_image(id="attachment-image", classes="attachment-image")
                img.display = False
                yield img
            else:
                yield Static("", id="attachment-image", classes="attachment-image")
            yield Static("", id="attachment-info")
            with Horizontal(id="attachment-modal-nav"):
                yield Button(f"‹ {tr('modal.previous')}", id="attachment-previous", disabled=len(self.attachments) < 2)
                yield Button(f"{tr('modal.next')} ›", id="attachment-next", disabled=len(self.attachments) < 2)
            yield Static(tr("attachments.browse_hint"), id="attachment-modal-hint")

    def on_mount(self) -> None:
        signal = getattr(self.app, "theme_changed_signal", None)
        if signal is not None:
            signal.subscribe(self, self._on_theme_changed)
        self._refresh()

    def _on_theme_changed(self, _theme: object = None) -> None:
        if not self.is_attached or not self.is_mounted:
            return
        if self._raw_image is not None:
            self._paint_image(self._raw_image)

    def relocalize(self) -> None:
        self.query_one("#attachment-modal-title", Static).update(
            f"{tr('attachments.title')}  [$dim]{visual_markup(self.save_name)}[/]"
        )
        self.query_one("#attachment-previous", Button).label = f"‹ {tr('modal.previous')}"
        self.query_one("#attachment-next", Button).label = f"{tr('modal.next')} ›"
        self.query_one("#attachment-modal-hint", Static).update(tr("attachments.browse_hint"))
        if self.attachments:
            self.query_one("#attachment-summary", Static).update(
                f"[$muted]{count_label(self.attachments)}  ·  {self.index + 1}/{len(self.attachments)}[/]"
            )

    def _refresh(self) -> None:
        if not self.attachments:
            return
        self._load_token += 1
        self._raw_image = None
        attachment = self.attachments[self.index]
        self.query_one("#attachment-summary", Static).update(
            f"[$muted]{count_label(self.attachments)}  ·  {self.index + 1}/{len(self.attachments)}[/]"
        )
        self.query_one("#attachment-info", Static).update(
            f"[$accent]{visual_markup(attachment.name)}[/]  ·  [$muted]{visual_markup(attachment.kind)}[/]"
        )
        target = self.query_one("#attachment-image")
        status = self.query("#attachment-status")
        if status:
            target.image = None
            target.display = False
            status.first(Static).update(tr("attachments.loading"))
            status.first().display = True
        elif isinstance(target, Static):
            target.update(tr("attachments.loading"))
        else:
            target.image = None
        self._load_image(self._load_token, attachment)

    @work(thread=True, exclusive=True)
    def _load_image(self, token: int, attachment: Attachment) -> None:
        try:
            image = _open_cover(_cover_bytes(attachment.url, attachment.headers, self.proxy))
        except Exception:
            try:
                self.app.call_from_thread(self._show_image, token, None)
            except RuntimeError:
                pass
            return
        try:
            self.app.call_from_thread(self._show_image, token, image)
        except RuntimeError:
            pass

    def _paint_image(self, image: Image.Image) -> None:
        target = self.query_one("#attachment-image")
        status = self.query("#attachment-status")
        if status:
            status.first().display = False
            target.display = True
        if protocol_image_class() is not None and not isinstance(target, Static):
            palette = getattr(self.app, "palette", None)
            # #attachment-modal paints on $recessed (palette.panel), not $base.
            bg = _hex_rgb(getattr(palette, "panel", None) or getattr(palette, "bg", None))
            target_width = int(getattr(target.size, "width", 0) or 0)
            target_height = int(getattr(target.size, "height", 0) or 0)
            if target_width > 0 and target_height > 0:
                target.image = _letterbox_image(image, target_width, target_height, background=bg)
            else:
                max_height = max(6, min(24, int(self.size.height) - 10))
                max_width = max(16, min(COVER_WIDTH, int(self.size.width) - 8))
                target.image = _contain_image(image, max_width, max_height, background=bg)
        else:
            max_height = max(6, min(24, int(self.size.height) - 10))
            max_width = max(16, min(COVER_WIDTH, int(self.size.width) - 8))
            width, height = _contain_dimensions(image, max_width, max_height)
            target.update(_cover_renderable(image, width=width, height=height), layout=False)

    def _show_image(self, token: int, image: Image.Image | None) -> None:
        if token != self._load_token or not self.is_mounted:
            return
        self._raw_image = image
        target = self.query_one("#attachment-image")
        status = self.query("#attachment-status")
        if image is None:
            if status:
                target.image = None
                target.display = False
                status.first(Static).update(tr("attachments.unavailable"))
                status.first().display = True
            elif isinstance(target, Static):
                target.update(tr("attachments.unavailable"))
            else:
                target.image = None
            return
        self._paint_image(image)

    def action_previous(self) -> None:
        if len(self.attachments) > 1:
            self.index = (self.index - 1) % len(self.attachments)
            self._refresh()

    def action_next(self) -> None:
        if len(self.attachments) > 1:
            self.index = (self.index + 1) % len(self.attachments)
            self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "attachment-previous":
            self.action_previous()
        elif event.button.id == "attachment-next":
            self.action_next()

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True

    def on_unmount(self) -> None:
        signal = getattr(self.app, "theme_changed_signal", None)
        if signal is not None:
            signal.unsubscribe(self)
        self._raw_image = None
        try:
            target = self.query_one("#attachment-image")
            if protocol_image_class() is not None and not isinstance(target, Static):
                target.image = None
        except Exception:
            pass


__all__ = ["AttachmentsScreen"]

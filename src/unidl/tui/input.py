"""Input and TextArea widgets with native system clipboard support."""

from __future__ import annotations

from textual import events
from textual.actions import SkipAction
from textual.widgets import Input as TextualInput
from textual.widgets import TextArea as TextualTextArea

from ..core.i18n import is_rtl_locale
from .bidi import has_unshaped_arabic, shape_strip
from .clipboard import read_clipboard, write_system_clipboard


class ClipboardInput(TextualInput):
    """Textual Input whose copy/cut/paste actions also talk to the OS clipboard."""

    @staticmethod
    def _single_line(value: str) -> str:
        """Keep one-line fields safe when a terminal pastes a document."""
        if not value:
            return ""
        lines = str(value).splitlines()
        if not lines:
            return ""
        for line in lines:
            if line.strip():
                return line
        return lines[0]

    def _insert_paste(self, value: str) -> None:
        value = self._single_line(value)
        if not value:
            return
        start, end = self.selection
        self.replace(value, start, end)

    def on_paste(self, event: events.Paste) -> None:
        """Insert bracketed/native paste events sent by Windows Terminal."""
        event.stop()
        event.prevent_default()
        self._insert_paste(event.text)

    def action_paste(self) -> None:
        clipboard = read_clipboard(self.app)
        self._insert_paste(clipboard)

    def action_copy(self) -> None:
        selected_text = self.selected_text
        if selected_text:
            self.app.copy_to_clipboard(selected_text)
            write_system_clipboard(selected_text)
        else:
            raise SkipAction()

    def action_cut(self) -> None:
        selected_text = self.selected_text
        self.app.copy_to_clipboard(selected_text)
        if selected_text:
            write_system_clipboard(selected_text)
        self.delete_selection()

    def render_line(self, y: int):
        strip = super().render_line(y)
        if y == 0 and not self.value and self.placeholder and has_unshaped_arabic(self.placeholder):
            rtl = is_rtl_locale()
            if rtl and self.content_size.width > 0 and strip.cell_length > self.content_size.width:
                # Textual's Input appends one extra trailing cell for the
                # end-of-line cursor; crop to the visible content width first so
                # right-aligning does not push the first Arabic letter into that
                # clipped overflow cell.
                strip = strip.crop(0, self.content_size.width)
            return shape_strip(strip, rtl_base=rtl, align_right=rtl)
        return strip


class ClipboardTextArea(TextualTextArea):
    """Textual TextArea whose copy/cut/paste actions also talk to the OS clipboard."""

    @staticmethod
    def _normalize_newlines(value: str) -> str:
        return str(value or "").replace("\r\n", "\n").replace("\r", "\n")

    def _insert_paste(self, value: str) -> None:
        if self.read_only:
            return
        text = self._normalize_newlines(value)
        if not text:
            return
        if result := self._replace_via_keyboard(text, *self.selection):
            self.move_cursor(result.end_location)

    async def _on_paste(self, event: events.Paste) -> None:
        """Insert bracketed/native paste events cleanly across OS line endings."""
        event.stop()
        event.prevent_default()
        self._insert_paste(event.text)
        if not self.read_only:
            self.focus()

    def action_paste(self) -> None:
        self._insert_paste(read_clipboard(self.app))

    def action_copy(self) -> None:
        selected_text = self.selected_text
        if selected_text:
            self.app.copy_to_clipboard(selected_text)
            write_system_clipboard(selected_text)
        else:
            raise SkipAction()

    def action_cut(self) -> None:
        if self.read_only:
            return
        start, end = self.selection
        if start == end:
            edit_result = self._delete_cursor_line()
        else:
            edit_result = self._delete_via_keyboard(start, end)
        if edit_result is not None:
            self.app.copy_to_clipboard(edit_result.replaced_text)
            write_system_clipboard(edit_result.replaced_text)

    def render_line(self, y: int):
        strip = super().render_line(y)
        if not self.text and self.placeholder and has_unshaped_arabic(self.placeholder):
            rtl = is_rtl_locale()
            return shape_strip(strip, rtl_base=rtl, align_right=rtl)
        return strip


__all__ = ["ClipboardInput", "ClipboardTextArea"]

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from unidl.tui.input import ClipboardInput


class _ClipboardApp(App[None]):
    def compose(self) -> ComposeResult:
        yield ClipboardInput()


def test_ctrl_v_pastes_the_native_clipboard_value(monkeypatch) -> None:
    """Ctrl+V must use the platform clipboard, not only Textual's cache."""

    monkeypatch.setattr(
        "unidl.tui.input.read_clipboard",
        lambda _app: "https://example.test/video\nignored line",
    )

    async def exercise() -> None:
        app = _ClipboardApp()
        async with app.run_test() as pilot:
            field = app.query_one(ClipboardInput)
            field.focus()
            await pilot.press("ctrl+v")
            assert field.value == "https://example.test/video"

    asyncio.run(exercise())


def test_ctrl_v_replaces_the_selected_text(monkeypatch) -> None:
    monkeypatch.setattr("unidl.tui.input.read_clipboard", lambda _app: "new value")

    async def exercise() -> None:
        app = _ClipboardApp()
        async with app.run_test() as pilot:
            field = app.query_one(ClipboardInput)
            field.value = "old value"
            field.selection = (0, 3)
            field.focus()
            await pilot.press("ctrl+v")
            assert field.value == "new value value"

    asyncio.run(exercise())

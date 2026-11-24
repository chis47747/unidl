"""Settings panel, reachable from anywhere with ``s``.

Three sections, in order of how specific they are:

1. **Global** - what happens on pick, command export, debug. Same everywhere.
2. **<Service>** - that service's own vocabulary: provider profile, region or
   market names. Only shown
   when a service is active, and services can declare as many of these as they
   need; there is no fixed set.
3. **Tracks and output** - the shared quality vocabulary, applied against the
   real ladder after the manifest is parsed.

Edits persist immediately, so they take effect on the next request without a
restart. Settings that invalidate a login are marked and drop the session.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..core import cdmrules, naming, template, vaults
from ..core.i18n import (
    option_label,
    phrase,
    setting_help,
    setting_label,
    setting_value,
    tr,
)
from ..core.service import Service
from ..core.settings import Option as SettingOption
from ..core.settings import Setting, Settings, cookie_profile_setting
from . import banner
from .bidi import escape_markup, visual_markup
from .chrome import Chrome, CloseMark, KeyBar, fit_modal_card
from .input import ClipboardInput as Input
from .settings_layout import SettingRow, label_width, setting_row
from .theme import (
    ACCENT_PRESETS,
    DEFAULT_ACCENT,
    DEFAULT_TITLE_COLOR,
    normalize_accent,
    normalize_title_color,
    palette_for,
    swatch_markup,
    title_color_stops,
)

APPEARANCE_SETTING_KEYS = frozenset({"theme", "accent_color", "title_color"})


def appearance_swatch_markup(
    setting_key: str,
    value: Any,
    *,
    dark: bool,
    accent: str = DEFAULT_ACCENT,
) -> str:
    """Colour swatch representing a theme, system accent, or title colour choice."""
    accent_key = normalize_accent(accent)
    if setting_key == "theme":
        mode = "light" if str(value or "").lower() == "light" else "dark"
        pal = palette_for(mode, accent_key)
        surface = "#3a3a3a" if mode == "dark" else "#d4d0c7"
        return f"[{surface}]━━[/][{pal.accent}]━━[/]"
    if setting_key == "accent_color":
        preset = ACCENT_PRESETS[normalize_accent(str(value or ""))]
        return swatch_markup(preset.swatch_stops(dark), blocks=4)
    if setting_key == "title_color":
        stops = title_color_stops(str(value or ""), dark=dark, accent=accent_key)
        return swatch_markup(stops, blocks=4)
    return ""


class _Editor(ModalScreen[Any]):
    """Base for centered Vault-style setting editor popups."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(self, setting: Setting, current: Any):
        super().__init__()
        self.setting = setting
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-card"):
            yield from self.compose_title()
            help_text = setting_help(self.setting)
            if help_text:
                yield Label(help_text, classes="modal-card-help")
            yield from self.compose_editor()
            yield from self.compose_actions()

    def compose_title(self) -> ComposeResult:
        with Horizontal(classes="modal-head"):
            yield Label(setting_label(self.setting), classes="modal-card-title")
            yield CloseMark(id="modal-close")

    def compose_editor(self) -> ComposeResult:
        yield from ()

    def compose_actions(self) -> ComposeResult:
        with Horizontal(classes="modal-card-actions"):
            yield Button(phrase("Cancel"), id="modal-cancel")

    def on_resize(self) -> None:
        self._fit_modal()

    def _fit_modal(self) -> None:
        fit_modal_card(self, "#modal-card", "OptionList")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Textual invokes ``on_*`` handlers across the entire MRO. Keep a single
        # handler on ``_Editor`` and delegate to ``_handle_button`` so clicking
        # Cancel in a subclass never fires ``dismiss`` twice and pops the parent
        # ``SettingsScreen`` as well.
        event.stop()
        self._handle_button(event.button.id or "")

    def _handle_button(self, button_id: str) -> None:
        if button_id == "modal-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        if self.app.screen is self:
            self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class _ChoiceEditor(_Editor):
    def compose_editor(self) -> ComposeResult:
        yield OptionList(
            *[
                Option(
                    f"{'>' if option.value == self.current else ' '} {option_label(self.setting, option)}",
                    id=str(index),
                )
                for index, option in enumerate(self.setting.options)
            ]
        )

    def on_mount(self) -> None:
        option_list = self.query_one(OptionList)
        for index, option in enumerate(self.setting.options):
            if option.value == self.current:
                option_list.highlighted = index
                break
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.setting.options[int(event.option.id or 0)].value)


class _PreviewOptionList(OptionList):
    """OptionList where mouse clicks highlight for live preview without committing."""

    def on_click(self, event) -> None:
        clicked_option = event.style.meta.get("option")
        if clicked_option is None:
            return
        event.prevent_default()
        event.stop()
        if not self._options[clicked_option].disabled:
            self.highlighted = clicked_option


class _AppearanceChoiceEditor(_Editor):
    """Choice editor for theme, system accent, and title colour with live preview."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "apply", "Apply", show=False, priority=True),
    ]

    def __init__(self, setting: Setting, current: Any):
        super().__init__(setting, current)
        self._committed = False
        self._previewed: Any = current

    def compose_editor(self) -> ComposeResult:
        with Vertical(id="appearance-preview"):
            yield Static("", id="appearance-preview-banner")
            yield Static("", id="appearance-preview-ui")
            yield Static(
                tr(
                    "appearance.live_preview",
                    default="Click or ↑↓ to preview · Apply or Enter to save · Esc to cancel",
                ),
                id="appearance-preview-caption",
            )
        yield _PreviewOptionList(id="appearance-options")

    def compose_actions(self) -> ComposeResult:
        with Horizontal(classes="modal-card-actions"):
            yield Button(phrase("Apply"), variant="primary", id="modal-apply")
            yield Button(phrase("Cancel"), id="modal-cancel")

    def _fit_modal(self) -> None:
        fit_modal_card(self, "#modal-card", "#appearance-options")

    def _preview_state(self, value: Any) -> tuple[str, str, str]:
        mode = str(getattr(self.app, "theme_preference", "dark") or "dark").lower()
        accent = normalize_accent(getattr(self.app, "accent_preference", DEFAULT_ACCENT))
        title_color = normalize_title_color(
            getattr(self.app, "title_color_preference", DEFAULT_TITLE_COLOR)
        )
        if self.setting.key == "theme":
            mode = "light" if str(value or "").lower() == "light" else "dark"
        elif self.setting.key == "accent_color":
            accent = normalize_accent(str(value or ""))
        elif self.setting.key == "title_color":
            title_color = normalize_title_color(str(value or ""))
        return mode, accent, title_color

    def _option_prompts(self, value: Any) -> list[str]:
        mode, accent, _title_color = self._preview_state(value)
        dark = mode != "light"
        prompts: list[str] = []
        for option in self.setting.options:
            mark = "▸" if option.value == self._previewed else " "
            swatch = appearance_swatch_markup(
                self.setting.key,
                option.value,
                dark=dark,
                accent=accent,
            )
            label = escape_markup(option_label(self.setting, option))
            prompts.append(f"{mark} {swatch}  {label}" if swatch else f"{mark} {label}")
        return prompts

    def _rebuild_options(self, value: Any) -> None:
        options = self.query_one("#appearance-options", OptionList)
        prompts = self._option_prompts(value)
        if options.option_count == len(prompts):
            for idx, prompt in enumerate(prompts):
                options.replace_option_prompt_at_index(idx, prompt)
            return
        options.clear_options()
        for index, prompt in enumerate(prompts):
            options.add_option(Option(prompt, id=str(index)))

    def _render_preview(self, value: Any) -> None:
        mode, accent, title_color = self._preview_state(value)
        pal = palette_for(mode, accent)
        stops = title_color_stops(title_color, dark=pal.dark, accent=accent)
        viewport_h = self.size.height or self.app.size.height
        compact = bool(viewport_h and viewport_h < 26)
        self.query_one("#appearance-preview-banner", Static).update(
            banner.mini_art(stops, compact=compact)
        )
        prism = pal.prism
        k1 = prism[0] if prism else pal.accent
        k2 = prism[1 % len(prism)] if prism else pal.accent
        k3 = prism[2 % len(prism)] if prism else pal.accent
        settings_word = escape_markup(phrase("Settings"))
        apply_word = escape_markup(phrase("Apply"))
        sample = (
            f"[{k1}]◆[/] [{pal.fg}]unidl[/]"
            f"   [{pal.gutter}]·[/]   [{k2}]^s[/] [{pal.muted}]{settings_word}[/]"
            f"   [{pal.gutter}]·[/]   [{k3}]✓[/] [{pal.accent}]{apply_word}[/]"
        )
        self.query_one("#appearance-preview-ui", Static).update(sample)

    def _apply_live_preview(self, value: Any, *, repaint_app: bool = True) -> None:
        self._previewed = value
        if repaint_app:
            preview_fn = getattr(self.app, "set_appearance_preview", None)
            if callable(preview_fn):
                if self.setting.key == "theme":
                    preview_fn(theme=str(value))
                elif self.setting.key == "accent_color":
                    preview_fn(accent=str(value))
                elif self.setting.key == "title_color":
                    preview_fn(title_color=str(value))
        self._render_preview(value)
        self._rebuild_options(value)

    def _restore_preview(self) -> None:
        clear_fn = getattr(self.app, "clear_appearance_preview", None)
        if callable(clear_fn):
            clear_fn()

    def _highlighted_value(self) -> Any:
        options = self.query_one("#appearance-options", OptionList)
        index = options.highlighted
        if index is not None and 0 <= index < len(self.setting.options):
            return self.setting.options[index].value
        return self._previewed

    def _commit(self, value: Any) -> None:
        self._committed = True
        if self.app.screen is self:
            self.dismiss(value)

    def _handle_button(self, button_id: str) -> None:
        if button_id == "modal-apply":
            self.action_apply()
        else:
            self.action_cancel()

    def on_mount(self) -> None:
        self.query_one("#modal-card").add_class("appearance-modal")
        self._rebuild_options(self.current)
        options = self.query_one("#appearance-options", OptionList)
        for index, option in enumerate(self.setting.options):
            if option.value == self.current:
                options.highlighted = index
                break
        self._render_preview(self.current)
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        options.focus()

    def on_resize(self) -> None:
        self._render_preview(self._previewed)
        self._fit_modal()

    def on_unmount(self) -> None:
        if not self._committed:
            self._restore_preview()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        event.stop()
        index = int(event.option.id or 0)
        if 0 <= index < len(self.setting.options):
            self._apply_live_preview(self.setting.options[index].value)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        index = int(event.option.id or 0)
        if 0 <= index < len(self.setting.options):
            self._commit(self.setting.options[index].value)

    def action_apply(self) -> None:
        self._commit(self._highlighted_value())

    def action_cancel(self) -> None:
        self._restore_preview()
        super().action_cancel()


class _MultiChoiceEditor(_Editor):
    """Checkbox editor for service API profiles/resolutions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "apply", "Apply", show=False, priority=True),
        Binding("space", "toggle", "Toggle", show=False),
        Binding("a", "apply", "Apply", show=False),
    ]

    def __init__(self, setting: Setting, current: Any):
        super().__init__(setting, current)
        self.selected = list(setting.coerce(current))

    def _apply_label(self) -> str:
        return f"{phrase('Apply')} ({len(self.selected)})"

    def compose_editor(self) -> ComposeResult:
        yield OptionList(id="multi-setting-options")

    def compose_actions(self) -> ComposeResult:
        with Horizontal(classes="modal-card-actions"):
            yield Button(self._apply_label(), variant="primary", id="modal-apply")
            yield Button(phrase("Cancel"), id="modal-cancel")

    def _handle_button(self, button_id: str) -> None:
        if button_id == "modal-apply":
            self.action_apply()
        else:
            self.action_cancel()

    def on_mount(self) -> None:
        self._rebuild_options()
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        self.query_one("#multi-setting-options", OptionList).focus()

    def _rebuild_options(self) -> None:
        options = self.query_one("#multi-setting-options", OptionList)
        prompts: list[str] = [
            f"  [$accent]{tr('settings.use_n', count=len(self.selected))}[/]",
        ]
        for item in self.setting.options:
            checked = item.value in self.selected
            mark = "[x]" if checked else "[ ]"
            role = "accent" if checked else "muted"
            prompts.append(
                f"  [${role}]{escape_markup(mark)}[/]  {escape_markup(option_label(self.setting, item))}"
            )
        if options.option_count == len(prompts):
            for idx, prompt in enumerate(prompts):
                options.replace_option_prompt_at_index(idx, prompt)
        else:
            previous = options.highlighted
            options.clear_options()
            options.add_option(Option(prompts[0], id="apply"))
            for index, prompt in enumerate(prompts[1:]):
                options.add_option(Option(prompt, id=f"value-{index}"))
            options.highlighted = min(previous or 0, len(self.setting.options))
        apply_buttons = self.query("#modal-apply")
        if apply_buttons:
            apply_buttons.first(Button).label = self._apply_label()

    def _toggle_index(self, index: int | None) -> None:
        if index is None or not 0 < index <= len(self.setting.options):
            return
        option = self.setting.options[index - 1]
        if option.value in self.selected:
            self.selected.remove(option.value)
        elif option.exclusive:
            self.selected = [option.value]
        else:
            self.selected = [
                value for value in self.selected
                if not any(item.value == value and item.exclusive for item in self.setting.options)
            ]
            self.selected.append(option.value)
        self._rebuild_options()
        self.query_one("#multi-setting-options", OptionList).highlighted = index

    def action_toggle(self) -> None:
        self._toggle_index(
            self.query_one("#multi-setting-options", OptionList).highlighted
        )

    def action_apply(self) -> None:
        self.dismiss(tuple(self.selected))

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        if event.option.id == "apply":
            self.action_apply()
            return
        self._toggle_index(event.option_index)


class _DefaultAudioEditor(_Editor):
    """A single preference: two presets or one user-entered language."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def compose_editor(self) -> ComposeResult:
        yield OptionList(
            Option(tr("setting.default_audio.option.auto"), id="auto"),
            Option(tr("setting.default_audio.option.original"), id="original"),
            Option(tr("mux_audio.specify_language"), id="language"),
            id="default-audio-options",
        )
        yield Input(
            value=str(self.current or "") if self.current not in {"auto", "original", None} else "",
            placeholder="en / fr-CA / es-419",
            classes="ask-input",
            id="default-audio-language",
        )

    def compose_actions(self) -> ComposeResult:
        with Horizontal(classes="modal-card-actions"):
            yield Button(phrase("Save"), variant="primary", id="modal-save")
            yield Button(phrase("Cancel"), id="modal-cancel")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        field = self.query_one(Input)
        custom = self.current not in {"auto", "original", None}
        options.highlighted = 2 if custom else (1 if self.current == "original" else 0)
        field.display = custom
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        (field if custom else options).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id in {"auto", "original"}:
            self.dismiss(event.option.id)
            return
        field = self.query_one(Input)
        field.display = True
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        field.focus()

    def _submit_value(self, raw: str) -> None:
        from ..downloader.mux_audio import normalize_default_audio

        try:
            value = normalize_default_audio(raw)
        except ValueError:
            self.notify(tr("mux_audio.invalid_language"), severity="warning")
            return
        self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit_value(event.value)

    def action_save(self) -> None:
        field = self.query_one(Input)
        if field.display:
            self._submit_value(field.value)
            return
        options = self.query_one(OptionList)
        idx = options.highlighted
        if idx in (0, 1):
            self.dismiss("auto" if idx == 0 else "original")
            return
        field.display = True
        field.focus()

    def _handle_button(self, button_id: str) -> None:
        if button_id == "modal-save":
            self.action_save()
        else:
            self.action_cancel()


class _DefaultSubtitleEditor(_Editor):
    """A single subtitle preference: presets or one language tag."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def compose_editor(self) -> ComposeResult:
        yield OptionList(
            Option(tr("setting.default_subtitle.option.auto"), id="auto"),
            Option(tr("setting.default_subtitle.option.original"), id="original"),
            Option(tr("mux_subtitle.specify_language"), id="language"),
            id="default-subtitle-options",
        )
        yield Input(
            value=str(self.current or "") if self.current not in {"auto", "original", None} else "",
            placeholder="en / fr-CA / es-419",
            classes="ask-input",
            id="default-subtitle-language",
        )

    def compose_actions(self) -> ComposeResult:
        with Horizontal(classes="modal-card-actions"):
            yield Button(phrase("Save"), variant="primary", id="modal-save")
            yield Button(phrase("Cancel"), id="modal-cancel")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        field = self.query_one(Input)
        custom = self.current not in {"auto", "original", None}
        options.highlighted = 2 if custom else (1 if self.current == "original" else 0)
        field.display = custom
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        (field if custom else options).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id in {"auto", "original"}:
            self.dismiss(event.option.id)
            return
        field = self.query_one(Input)
        field.display = True
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        field.focus()

    def _submit_value(self, raw: str) -> None:
        from ..downloader.mux_subtitle import normalize_default_subtitle

        try:
            value = normalize_default_subtitle(raw)
        except ValueError:
            self.notify(tr("mux_subtitle.invalid_language"), severity="warning")
            return
        self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit_value(event.value)

    def action_save(self) -> None:
        field = self.query_one(Input)
        if field.display:
            self._submit_value(field.value)
            return
        options = self.query_one(OptionList)
        idx = options.highlighted
        if idx in (0, 1):
            self.dismiss("auto" if idx == 0 else "original")
            return
        field.display = True
        field.focus()

    def _handle_button(self, button_id: str) -> None:
        if button_id == "modal-save":
            self.action_save()
        else:
            self.action_cancel()


_SPEED_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b(?:ps|/s)?|ps)?$", re.IGNORECASE)
_INT_MINIMUMS = {
    "retries": 0,
    "http_timeout": 1,
    "workers": 1,
}


def validate_setting_value(spec: Setting, value: Any) -> list[str]:
    """Validate free-text and numeric setting values before persisting them."""
    problems = _template_problems(spec, value)
    if problems:
        return problems
    text = str(value or "").strip()
    if spec.kind == "int":
        try:
            number = int(text)
        except ValueError:
            return [tr("settings.invalid_int", default="Enter a whole number.")]
        minimum = _INT_MINIMUMS.get(spec.key)
        if minimum is not None and number < minimum:
            return [tr("settings.invalid_min", default=f"Must be at least {minimum}.", min=minimum)]
    elif spec.key == "drop_video" and text:
        try:
            re.compile(text)
        except re.error as exc:
            return [tr("settings.invalid_regex", default=f"Invalid regular expression: {exc}", error=str(exc))]
    elif spec.key == "max_speed" and text:
        match = _SPEED_RE.match(text)
        if not match or float(match.group(1)) <= 0:
            return [tr("settings.invalid_speed", default="Use a positive rate such as 15M, 100K, or 2Mbps (or leave empty).")]
    return []


class _TextEditor(_Editor):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def compose_editor(self) -> ComposeResult:
        yield Input(
            value="" if self.current is None else str(self.current),
            placeholder=tr("settings.placeholder"),
            classes="ask-input",
        )

    def compose_actions(self) -> ComposeResult:
        with Horizontal(classes="modal-card-actions"):
            yield Button(phrase("Save"), variant="primary", id="modal-save")
            yield Button(phrase("Cancel"), id="modal-cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _submit(self, value: str) -> None:
        problems = validate_setting_value(self.setting, value)
        if problems:
            self.notify(
                "  ·  ".join(problems),
                title=tr("settings.not_saved", label=setting_label(self.setting)),
                severity="warning",
                timeout=8,
            )
            return
        self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit(event.value)

    def action_save(self) -> None:
        self._submit(self.query_one(Input).value)

    def _handle_button(self, button_id: str) -> None:
        if button_id == "modal-save":
            self.action_save()
        else:
            self.action_cancel()


class _TitleDelayEditor(_Editor):
    """Validate the batch-only inter-title delay before saving it."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def compose_editor(self) -> ComposeResult:
        yield Input(
            value="" if self.current is None else str(self.current),
            placeholder="off / 30 / 20-40",
            classes="ask-input",
        )

    def compose_actions(self) -> ComposeResult:
        with Horizontal(classes="modal-card-actions"):
            yield Button(phrase("Save"), variant="primary", id="modal-save")
            yield Button(phrase("Cancel"), id="modal-cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _submit(self, raw: str) -> None:
        from ..core.title_delay import normalize_title_delay

        try:
            value = normalize_title_delay(raw)
        except ValueError as exc:
            self.notify(
                tr(
                    "title_delay.invalid",
                    default=f"Invalid title delay: {exc}",
                    error=str(exc),
                ),
                severity="warning",
            )
            return
        self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit(event.value)

    def action_save(self) -> None:
        self._submit(self.query_one(Input).value)

    def _handle_button(self, button_id: str) -> None:
        if button_id == "modal-save":
            self.action_save()
        else:
            self.action_cancel()


#: Which settings are name templates, and which fields each may use. A table rather
#: than a flag on Setting: it is two facts about three settings, and the alternative
#: was a validator hook on every setting there has ever been.
TEMPLATE_FIELDS = {
    "name_template_episode": naming.TITLE_FIELDS,
    "name_template_movie": naming.TITLE_FIELDS,
    "release_template": naming.RELEASE_FIELDS,
}


def _template_problems(spec: Setting, value: Any) -> list[str]:
    """What is wrong with this value, if the setting is a name template."""
    allowed = TEMPLATE_FIELDS.get(spec.key)
    if allowed is None:
        return []
    return template.validate(str(value or ""), allowed)


class SettingsScreen(Screen):
    BINDINGS = [
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "noop", "Change", show=True),
        Binding("r", "reset", "Reset", show=True),
    ]

    def action_noop(self) -> None:
        """Footer-only hint; Enter is handled by the focused list."""

    def __init__(self, service: Service | None, globals_scope: Settings, *, focus_key: str = ""):
        super().__init__()
        self.service = service
        self.globals = globals_scope
        #: the settings scope edits are written through
        self.scope = service.settings if service is not None else globals_scope
        self._rows: list[tuple[Setting, Settings] | None] = []
        #: whether the list was last built with the blank rows between settings
        self._roomy = True
        self._label_width = 0
        self._focus_key = focus_key
        self._search_key = focus_key

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static("", id="masthead")
        with Horizontal(id="statusline"):
            yield Static("", id="crumbs-pill", classes="pill")
        with Vertical(id="body"):
            yield OptionList(id="settings-list")
        yield KeyBar(("enter", "change"), ("r", "reset"), ("^b", "back"), ("esc", "quit"))

    def on_mount(self) -> None:
        if self.service is None:
            title = f"{tr('settings.title')}  [$dim]{tr('settings.global')}[/]"
        else:
            title = f"{tr('settings.title')}  [$dim]{visual_markup(self.service.NAME)}[/]"
        self.query_one("#masthead", Static).update(title)
        self.set_hint("")
        self.rebuild()
        self.query_one("#settings-list", OptionList).focus()

    def relocalize(self) -> None:
        from .chrome import Chrome, KeyBar

        for chrome in self.query(Chrome):
            chrome.refresh_locale()
        for bar in self.query(KeyBar):
            bar.render_pairs()
        if self.service is None:
            title = f"{tr('settings.title')}  [$dim]{tr('settings.global')}[/]"
        else:
            title = f"{tr('settings.title')}  [$dim]{visual_markup(self.service.NAME)}[/]"
        found = self.query("#masthead")
        if found:
            found.first(Static).update(title)
        self.rebuild()

    def set_hint(self, text: str) -> None:
        note = f"[$dim]{escape_markup(text)}[/]" if text else ""
        self.query_one("#crumbs-pill", Static).update(note)

    def _refresh_cookie_setting(self, spec: Setting, scope: Settings) -> None:
        """Re-read this service's cookie directory before showing its choices."""
        if spec.picker != "cookie" or self.service is None:
            return
        cookie_service_id = self.service.COOKIE_SERVICE_ID or self.service.ID
        legacy_cookie_ids = () if self.service.COOKIE_SERVICE_ID else self.service.LEGACY_IDS
        fresh = cookie_profile_setting(cookie_service_id, self.app.config, legacy_cookie_ids)
        selected = str(scope.get(spec.key) or "").strip()
        if selected and all(option.value != selected for option in fresh.options):
            fresh.options.append(
                SettingOption(selected, tr("settings.cookie_missing", name=selected))
            )
        spec.options = fresh.options
        spec.help = fresh.help

    def on_resize(self) -> None:
        # Columns reflow at render time. Only rebuild when row spacing changes.
        if self.is_active:
            self.call_later(self._resize_spacing)

    def on_screen_resume(self) -> None:
        self.call_later(self._resize_spacing)

    def _resize_spacing(self) -> None:
        if self.is_active and self._roomy != (not self.app.has_class("short")):
            self.rebuild()

    # ------------------------------------------------------------------ render
    def rebuild(self) -> None:
        option_list = self.query_one("#settings-list", OptionList)
        # by key, not by index: every edit rebuilds this list, and the row that was
        # being edited is the row that should still be under the cursor afterwards -
        # which an index stops meaning as soon as a section appears or the spacing
        # changes
        selected = self._selected()
        wanted = selected[0].key if selected is not None else ""
        option_list.clear_options()
        self._rows = []

        roomy = not self.app.has_class("short")
        self._roomy = roomy

        # Keep the value column aligned even when a service declares a label
        # longer than the historical 34-cell baseline (for example, the remote
        # vault policy rows).  Rich markup does not participate in terminal
        # width, so measure the translated plain labels explicitly.
        visible_labels = [
            setting_label(spec)
            for spec in self.scope.specs
            if spec.visible
        ]
        self._label_width = label_width(visible_labels)

        def add_spacer() -> None:
            """A blank row between settings, when there is room for one.

            Textual ignores vertical padding on an option, so air between rows has
            to be a row. Disabled, so the cursor steps over it and Enter can never
            land on nothing. Dropped in a short window for the same reason the rest
            of the spacing is: four settings on screen instead of nine is a worse
            list than a dense one.
            """
            if not roomy:
                return
            self._rows.append(None)
            option_list.add_option(Option("", disabled=True))

        def add_section(name: str, specs: list[Setting], scope: Settings, note: str = "") -> None:
            visible_specs = [spec for spec in specs if spec.visible or spec.key == self._search_key]
            if not visible_specs:
                return
            self._rows.append(None)
            suffix = f"  [$gutter]{visual_markup(note)}[/]" if note else ""
            option_list.add_option(Option(f"  [$dim]{visual_markup(name.upper())}[/]{suffix}", disabled=True))
            for spec in visible_specs:
                self._refresh_cookie_setting(spec, scope)
                self._rows.append((spec, scope))
                option_list.add_option(Option(self._row_markup(spec, scope)))
                add_spacer()

        if self.service is None:
            # opened from the main screen: application-wide behaviour only
            add_section(tr("settings.section.global"), list(self.globals.specs), self.globals)
        else:
            # opened inside a service: only what applies to that service
            scope = self.service.settings
            service_note = tr("settings.note.this_service")
            policy_keys = {
                        "license_action", "license_after_tracks", "verify_content_keys", "dolby_vision_hybrid", "local_vault", "remote_vault",
                "remote_vault_auto_lookup", "remote_vault_auto_store",
                "vault_read_targets", "vault_write_targets",
            }
            declared_keys = {spec.key for spec in getattr(type(self.service), "SETTINGS", ())}
            add_section(
                self.service.NAME,
                [
                    spec
                    for spec in scope.service_specs()
                    if spec.key not in policy_keys or spec.key in declared_keys
                ],
                scope,
                note=service_note,
            )
            add_section(
                tr("settings.section.license_vaults"),
                [
                    spec for spec in scope.specs
                    if spec.key in {
                        "license_action",
                        "verify_content_keys",
                        "dolby_vision_hybrid",
                        "local_vault",
                        "remote_vault",
                        "remote_vault_auto_lookup",
                        "remote_vault_auto_store",
                        "vault_read_targets",
                        "vault_write_targets",
                    }
                    and spec.key not in declared_keys
                ],
                scope,
                note=service_note,
            )
            add_section(
                tr("settings.section.tracks"),
                scope.track_specs(),
                scope,
                note=service_note,
            )

        settings_rows = [
            index for index, row in enumerate(self._rows) if row is not None
        ]
        found = next(
            (
                index
                for index in settings_rows
                if (self._rows[index] or (None,))[0].key == wanted
            ),
            None,
        )
        if self._focus_key:
            found = next(
                (index for index in settings_rows if self._rows[index][0].key == self._focus_key),
                found,
            )
            self._focus_key = ""
        if found is not None:
            option_list.highlighted = found
            option_list.scroll_to_highlight()
        elif settings_rows:
            option_list.highlighted = settings_rows[0]

    def _row_markup(self, spec: Setting, scope: Settings) -> SettingRow:
        if spec.picker in {
            "resources",
            "storage",
            "proxy_manager",
            "justwatch",
            "services_manager",
            "download_behavior",
            "interface",
        }:
            value = phrase("open manager")
        elif spec.kind == "bool":
            value = setting_value(spec, scope)
        elif spec.picker == "cdm_rules":
            # before the branch below, which every `cdm...` picker falls into: this
            # value is a list of rules, not the name of one device
            value = cdmrules.summary(cdmrules.parse(scope.get(spec.key)))
        elif spec.picker.startswith("cdm"):
            value = self._cdm_label(scope.get(spec.key))
        elif spec.picker.startswith("vaults:"):
            value = self._vault_label(scope.get(spec.key), spec)
        elif spec.key == "download_dir" and not str(scope.get(spec.key) or "").strip():
            # An empty text row reads as "not set anywhere", and for this one the
            # question is always answered - by unidl.yaml when nothing was chosen
            # here. Showing the folder that will actually be used is the whole point
            # of the row: "where did my file go" is the question after every download.
            value = tr("settings.from_yaml", path=self.app.config.paths.downloads)
        elif spec.key == "drm_system" and scope is not self.globals and not scope.get(spec.key):
            # "Use the app-wide setting" hid the value that would actually be
            # used. That made a service inheriting PlayReady look like it was on
            # its first/default system (normally Widevine) until the licence log
            # said otherwise.
            value = f"{tr('settings.app_wide')} · {self.globals.label_for('drm_system')}"
        else:
            value = setting_value(spec, scope)
        # escaped: these values are paths, device file names and free text out of
        # unidl.yaml. A folder called "TV [new]" rendered as "TV " - and the editor
        # one keypress away showed the truth, so the row was the only thing lying.
        #
        row = f"[$accent]{escape_markup(value)}[/]"
        if spec.inherit_global and scope.parent is not None:
            source = "settings.service_override" if scope.has_override(spec.key) else "settings.follows_global"
            service_default = (
                spec.key == "license_action" and spec.default == "custom"
                and not scope.has_override(spec.key) and not scope.parent.has_override(spec.key)
            )
            if not service_default:
                row += f"  [$dim]{tr(source)}[/]"
        if spec.resets_session:
            row += f"  [$dim]{tr('settings.signs_out')}[/]"
        return setting_row(
            self.query_one("#settings-list", OptionList),
            setting_label(spec), row, self._label_width, indent=4,
        )

    def _cdm_label(self, selected: Any) -> str:
        """Name a selected device and whether the exchange is local or remote."""
        value = str(selected or "").strip()
        if not value:
            return tr("settings.cdm.automatic")
        # Include disabled entries here. A service-specific choice is allowed to
        # remain saved while its endpoint is parked in the shared manager; showing
        # it as a local file made the setting look silently rewritten, even though
        # the resolver correctly falls back until the endpoint is enabled again.
        remote = self.app.config.remote_cdm(value, include_disabled=True)
        if remote is not None:
            state = tr("settings.cdm.disabled") if not getattr(remote, "enabled", True) else ""
            return tr("settings.cdm.remote", name=remote.name) + state
        return tr("settings.cdm.local", name=Path(value).stem)

    def _vault_label(self, selected: Any, spec: Setting) -> str:
        descriptors = vaults.configured_vaults(self.app.config)
        if spec.picker.endswith(":write"):
            descriptors = [descriptor for descriptor in descriptors if descriptor.writable]
        elif spec.picker.endswith(":search"):
            descriptors = [descriptor for descriptor in descriptors if descriptor.searchable]
        parsed = vaults.parse_targets(selected)
        if parsed is None:
            ident = "settings.vault.all_writable" if spec.picker.endswith(":write") else "settings.vault.all_available"
            return tr(ident, count=len(descriptors))
        if not parsed:
            return tr("settings.vault.disabled")
        wanted = {name.casefold() for name in parsed}
        labels = [descriptor.name for descriptor in descriptors if descriptor.name.casefold() in wanted]
        labels += [name for name in parsed if name.casefold() not in {label.casefold() for label in labels}]
        if not labels:
            return tr("settings.vault.disabled")
        if len(labels) <= 2:
            return " + ".join(labels)
        return tr("settings.vault.more", name=labels[0], count=len(labels) - 1)

    def _selected(self) -> tuple[Setting, Settings] | None:
        option_list = self.query_one("#settings-list", OptionList)
        index = option_list.highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index]

    # -------------------------------------------------------------------- edit
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        selected = self._selected()
        if selected is None:
            return
        spec, scope = selected
        self._refresh_cookie_setting(spec, scope)

        if spec.kind == "bool":
            self._apply(spec, scope, not bool(scope.get(spec.key)))
            return

        def _done(value: Any) -> None:
            if value is None:
                return
            problems = validate_setting_value(spec, value)
            if problems:
                # Refused rather than saved and worked around later: a template with
                # a mistyped field in it puts that mistake in the name of every file
                # from here on, and the place to say so is where it was typed.
                self.notify(
                    "  ·  ".join(problems),
                    title=tr("settings.not_saved", label=setting_label(spec)),
                    severity="error",
                    timeout=10,
                )
                return
            self._apply(spec, scope, value)

        if spec.picker == "cdm_rules":
            from .cdm_rules_screen import CdmRulesScreen

            self.app.push_screen(CdmRulesScreen(str(scope.get(spec.key) or "")), _done)
            return

        if spec.picker == "resources":
            from .resource_manager import ResourceManagerScreen

            def finished(_result) -> None:
                # The manager can alter the dynamic vault definitions. Redraw the
                # rows on return so their summaries immediately name the current
                # backends rather than the snapshot that opened this screen.
                self.rebuild()

            self.app.push_screen(ResourceManagerScreen(), finished)
            return

        if spec.picker == "storage":
            from .storage_manager import StorageManagerScreen

            def finished(_result) -> None:
                # Path and template values keep their existing keys, but all of
                # their summaries live inside the manager. Rebuild the single
                # entry row after returning so the settings screen remains the
                # source of truth for navigation.
                self.rebuild()

            self.app.push_screen(StorageManagerScreen(self.globals), finished)
            return

        if spec.picker == "justwatch":
            from .justwatch_settings import JustWatchSettingsScreen

            self.app.push_screen(JustWatchSettingsScreen(self.globals), lambda _result: self.rebuild())
            return

        if spec.picker == "proxy_manager":
            from .proxy_manager import ProxyManagerScreen

            self.app.push_screen(ProxyManagerScreen(self.globals), lambda _result: self.rebuild())
            return

        if spec.picker == "services_manager":
            from .service_manager import ServicesManagerScreen

            self.app.push_screen(ServicesManagerScreen(self.globals), lambda _result: self.rebuild())
            return

        if spec.picker in {"download_behavior", "interface"}:
            from .settings_group import SETTINGS_GROUPS, SettingsGroupScreen

            title, description, keys = SETTINGS_GROUPS[spec.picker]
            self.app.push_screen(
                SettingsGroupScreen(self.globals, title, description, keys),
                lambda _result: self.rebuild(),
            )
            return

        if spec.picker.startswith("cdm"):
            self._pick_device(spec, scope, _done)
            return

        if spec.picker.startswith("vaults:"):
            self._pick_vaults(spec, scope, _done)
            return

        if spec.key == "title_delay":
            self.app.push_screen(_TitleDelayEditor(spec, scope.get(spec.key)), _done)
        elif spec.key == "default_audio":
            self.app.push_screen(_DefaultAudioEditor(spec, scope.get(spec.key)), _done)
        elif spec.key == "default_subtitle":
            self.app.push_screen(_DefaultSubtitleEditor(spec, scope.get(spec.key)), _done)
        elif spec.key in APPEARANCE_SETTING_KEYS and spec.options:
            self.app.push_screen(_AppearanceChoiceEditor(spec, scope.get(spec.key)), _done)
        elif spec.kind == "multi" and spec.options:
            self.app.push_screen(_MultiChoiceEditor(spec, scope.get(spec.key)), _done)
        elif spec.kind == "choice" and spec.options:
            self.app.push_screen(_ChoiceEditor(spec, scope.get(spec.key)), _done)
        else:
            self.app.push_screen(_TextEditor(spec, scope.get(spec.key)), _done)

    def _pick_device(self, spec: Setting, scope: Settings, done) -> None:
        """Choose a CDM with the real picker rather than a flat list.

        The same screen the main one uses, with two differences: it is restricted
        to the system this setting is for, and it carries a row meaning "no opinion
        here" - which is how a per-service device is handed back to the app-wide
        choice.
        """
        from .cdm_screen import CdmScreen

        system = spec.picker.partition(":")[2]
        active = system or str(self.scope.inherited("drm_system", "") or "widevine")
        self.app.push_screen(
            CdmScreen(
                current=str(scope.get(spec.key) or ""),
                system=active,
                only=system,
                allow_default=True,
                scope=self.service.NAME if self.service is not None else "",
            ),
            done,
        )

    def _pick_vaults(self, spec: Setting, scope: Settings, done) -> None:
        from .vault_targets import VaultTargetResult, VaultTargetScreen

        def finished(result: VaultTargetResult | None) -> None:
            if result is not None:
                done(vaults.serialize_targets(result.selected))

        descriptors = vaults.configured_vaults(self.app.config)
        if spec.picker.endswith(":search"):
            descriptors = [descriptor for descriptor in descriptors if descriptor.searchable]
        self.app.push_screen(
            VaultTargetScreen(
                descriptors,
                vaults.parse_targets(scope.get(spec.key)),
                title=setting_label(spec),
                writable_only=spec.picker.endswith(":write"),
            ),
            finished,
        )

    def _apply(self, spec: Setting, scope: Settings, value: Any) -> None:
        resets = scope.set(spec.key, value)
        store_problem = getattr(scope._store, "problem", "")
        if store_problem:
            if spec.key in APPEARANCE_SETTING_KEYS:
                clear_fn = getattr(self.app, "clear_appearance_preview", None)
                if callable(clear_fn):
                    clear_fn()
            self.notify(
                store_problem,
                title=tr("settings.not_saved", label=setting_label(spec)),
                severity="error",
                timeout=10,
            )
            self.rebuild()
            return
        if spec.key in APPEARANCE_SETTING_KEYS and scope is self.globals:
            clear_fn = getattr(self.app, "clear_appearance_preview", None)
            if callable(clear_fn):
                clear_fn(apply=False)
            self.app.apply_theme()
        paired_device = ""
        if spec.key == "drm_system" and scope is self.globals:
            # The main-screen DRM chip already moves the global CDM with the
            # system. The same setting changed through this screen used not to,
            # allowing e.g. PlayReady + a .wvd to be persisted. Keep both ways of
            # editing the same choice consistent.
            system = str(scope.get(spec.key) or "widevine")
            if self.app.active_device_system() != system:
                match = next((d for d in self.app.devices() if d.system == system), None)
                if match is not None:
                    self.app.set_device(match.name)
                    paired_device = match.name
                else:
                    self.notify(
                        tr("settings.no_cdm", label=setting_label(spec)),
                        severity="warning",
                        timeout=8,
                    )
        self.rebuild()
        if spec.picker == "cdm_rules":
            # the stored value is several lines; the pill is one
            label = cdmrules.summary(cdmrules.parse(scope.get(spec.key)))
        elif spec.picker.startswith("vaults:"):
            label = self._vault_label(scope.get(spec.key), spec)
        else:
            label = setting_value(spec, scope)
        note = f"{setting_label(spec)} -> {label}"
        if paired_device:
            note += tr("settings.paired_cdm", name=paired_device)
        if resets and self.service is not None:
            self.service.logout()
            note += tr("settings.signed_out")
        self.set_hint(note)

    def action_reset(self) -> None:
        selected = self._selected()
        if selected is not None:
            spec, scope = selected
            if spec.kind == "action":
                return
            if spec.inherit_global and scope is not self.globals:
                scope.clear(spec.key)
                store_problem = getattr(scope._store, "problem", "")
                if store_problem:
                    self.notify(
                        store_problem,
                        title=tr("settings.not_saved", label=setting_label(spec)),
                        severity="error",
                        timeout=10,
                    )
                    self.rebuild()
                    return
                self.rebuild()
                self.set_hint(tr("settings.inherited_global", label=setting_label(spec)))
                return
            self._apply(spec, scope, spec.default)

    def go_back(self) -> bool:
        # dismiss, not pop: Textual runs the callback this screen was pushed with
        # on dismiss and discards it on pop. ^b is how settings are normally
        # closed, and popping meant the theme was never re-applied, the login
        # hints were never invalidated, and an open session's screens were never
        # refreshed - everything `action_global_settings` does on the way out.
        self.dismiss(None)
        return True

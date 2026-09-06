"""Reusable multi-vault destination picker."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..core import vaults
from ..core.i18n import phrase, tr
from .bidi import visual_markup


class VaultCheckbox(Checkbox):
    """A quiet, terminal-friendly checkbox used by the vault pickers.

    Textual's stock toggle is intentionally button-like (``▐X▌``). That works
    well for a toolbar, but repeated twelve times in a destination list makes
    the modal look like a grid of controls. A ballot box keeps the same
    keyboard and mouse behaviour while making the row read as one selectable
    item.
    """

    @property
    def _button(self) -> Content:  # type: ignore[override]
        # ``ToggleButton._button`` is a property in Textual. Keep the property
        # shape here rather than using a static class glyph so the unchecked and
        # checked states remain obvious in monochrome terminals too.
        button_style = self.get_visual_style("toggle--button")
        # A one-cell ballot glyph is rendered with a large amount of internal
        # whitespace by common terminal fonts, so it looks much smaller than the
        # label beside it. A three-cell control remains unmistakable at normal
        # terminal scale and in monochrome mode.
        return Content.assemble(
            ("[✓]" if self.value else "[ ]", button_style),
        )


@dataclass(frozen=True)
class VaultTargetResult:
    """A confirmed selection; ``None`` means all available destinations."""

    selected: tuple[str, ...] | None


class VaultTargetScreen(ModalScreen[VaultTargetResult | None]):
    """Choose any number of vaults for one setting or manual write."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "apply", "Apply", show=False, priority=True),
    ]

    def __init__(
        self,
        descriptors: Iterable[vaults.VaultDescriptor],
        current: tuple[str, ...] | None,
        *,
        title: str = "",
        writable_only: bool = False,
    ) -> None:
        super().__init__()
        self.descriptors = [
            descriptor
            for descriptor in descriptors
            if not writable_only or descriptor.writable
        ]
        self.current = current
        self.title = title or tr("targets.title")
        self._boxes: list[Checkbox] = []
        self._all: Checkbox | None = None
        self._syncing = False

    def compose(self) -> ComposeResult:
        has_remote = any(descriptor.remote for descriptor in self.descriptors)
        remote_note = tr("targets.help_remote") if has_remote else ""
        with Vertical(id="vault-target-card"):
            yield Label(self.title, id="vault-target-title")
            yield Label(
                f"{tr('targets.help')}{remote_note}",
                id="vault-target-help",
            )
            with Horizontal(id="vault-target-summary"):
                yield VaultCheckbox(
                    tr("targets.all"),
                    id="vault-target-all",
                    disabled=not any(descriptor.enabled for descriptor in self.descriptors),
                )
                yield Static("", id="vault-target-count")
            with VerticalScroll(id="vault-target-list"):
                for remote in (False, True):
                    group = [
                        (index, descriptor)
                        for index, descriptor in enumerate(self.descriptors)
                        if descriptor.remote is remote
                    ]
                    if not group:
                        continue
                    group_name = tr("targets.group.remote") if remote else tr("targets.group.local")
                    yield Static(
                        f"[$dim]{group_name}[/]  [$gutter]{tr('targets.configured', count=len(group))}[/]",
                        classes="vault-target-group",
                    )
                    for index, descriptor in group:
                        yield VaultCheckbox(
                            self._descriptor_label(descriptor),
                            id=f"vault-target-{index}",
                            classes="vault-target-option",
                            disabled=not descriptor.enabled,
                        )
                if not self.descriptors:
                    yield Static(
                        f"[$muted]{tr('targets.empty')}[/]",
                        id="vault-target-empty",
                    )
            with Horizontal(id="vault-target-actions"):
                yield Button(phrase("Apply"), variant="primary", id="vault-target-apply")
                yield Button(phrase("Cancel"), id="vault-target-cancel")

    @staticmethod
    def _descriptor_label(descriptor: vaults.VaultDescriptor) -> str:
        """Render one compact row without exposing a long path-like sentence."""
        # The section header already says local or remote. Keep the row from
        # repeating that word and reserve the trailing badge for information a
        # user may need to act on.
        flags: list[str] = []
        if descriptor.remote and descriptor.searchable:
            flags.append(tr("targets.flag.search"))
        if not descriptor.writable:
            flags.append(tr("targets.flag.readonly"))
        if not descriptor.enabled:
            flags.append(tr("targets.flag.disabled"))
        suffix = f"  [$gutter]{' · '.join(flags)}[/]" if flags else ""
        return (
            f"[$foreground]{visual_markup(descriptor.name)}[/]  "
            f"[$muted]{visual_markup(descriptor.detail)}[/]"
            f"{suffix}"
        )

    def on_mount(self) -> None:
        self._all = self.query_one("#vault-target-all", Checkbox)
        self._boxes = [
            self.query_one(f"#vault-target-{index}", Checkbox)
            for index in range(len(self.descriptors))
        ]
        selected = None if self.current is None else {
            name.casefold() for name in self.current
        }
        for box, descriptor in zip(self._boxes, self.descriptors, strict=True):
            box.value = descriptor.enabled and (
                selected is None or descriptor.name.casefold() in selected
            )
        enabled_boxes = [box for box, descriptor in zip(self._boxes, self.descriptors, strict=True) if descriptor.enabled]
        self._all.value = bool(enabled_boxes) and (
            selected is None or all(box.value for box in enabled_boxes)
        )
        self._update_count()
        focusable = next(
            (box for box, descriptor in zip(self._boxes, self.descriptors, strict=True) if descriptor.enabled),
            self.query_one("#vault-target-cancel", Button),
        )
        focusable.focus()

    def _update_count(self) -> None:
        enabled = sum(descriptor.enabled for descriptor in self.descriptors)
        selected = sum(bool(box.value) for box, descriptor in zip(self._boxes, self.descriptors, strict=True) if descriptor.enabled)
        self.query_one("#vault-target-count", Static).update(
            tr("targets.count", selected=f"[$accent]{selected}[/]", enabled=enabled)
        )

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._syncing:
            return
        if event.checkbox is self._all:
            self._syncing = True
            try:
                for box, descriptor in zip(self._boxes, self.descriptors, strict=True):
                    if descriptor.enabled:
                        box.value = bool(event.value)
            finally:
                self._syncing = False
            self._update_count()
            return
        if event.checkbox in self._boxes and self._all is not None:
            self._syncing = True
            try:
                enabled_boxes = [
                    box
                    for box, descriptor in zip(self._boxes, self.descriptors, strict=True)
                    if descriptor.enabled
                ]
                self._all.value = bool(enabled_boxes) and all(box.value for box in enabled_boxes)
            finally:
                self._syncing = False
            self._update_count()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        if self._all is not None and self._all.value:
            self.dismiss(VaultTargetResult(None))
            return
        selected = tuple(
            descriptor.name
            for descriptor, box in zip(self.descriptors, self._boxes, strict=True)
            if descriptor.enabled and box.value
        )
        self.dismiss(VaultTargetResult(selected))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "vault-target-apply":
            self.action_apply()
        elif event.button.id == "vault-target-cancel":
            self.action_cancel()


class VaultSearchPicker(ModalScreen[str | None]):
    """Pick one remote vault for an explicit home-search request."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(self, descriptors: Iterable[vaults.VaultDescriptor]):
        super().__init__()
        self.descriptors = list(descriptors)

    def compose(self) -> ComposeResult:
        with Vertical(id="vault-target-card"):
            yield Label(tr("targets.search_title"), id="vault-target-title")
            yield Label(tr("targets.search_help"), id="vault-target-help")
            yield OptionList(
                *[
                    Option(
                        f"  [$foreground]{visual_markup(descriptor.name)}[/]  "
                        f"[$muted]{visual_markup(descriptor.detail)}[/]  "
                        f"[$gutter]{tr('targets.flag.search')}[/]"
                    )
                    for descriptor in self.descriptors
                ],
                id="vault-target-list",
            )
            with Horizontal(id="vault-target-actions"):
                yield Button(phrase("Cancel"), id="vault-target-cancel")

    def on_mount(self) -> None:
        option_list = self.query_one("#vault-target-list", OptionList)
        if option_list.option_count:
            option_list.highlighted = 0
            option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index < len(self.descriptors):
            self.dismiss(self.descriptors[index].name)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()


class VaultServicePicker(ModalScreen[str | None]):
    """Choose all or one declared service tag for one remote key search."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(self, vault_name: str, services: Iterable[tuple[str, str]]):
        super().__init__()
        self.vault_name = vault_name
        self.services = [
            (str(value).strip(), str(label).strip())
            for value, label in services
            if str(value).strip()
        ]

    def compose(self) -> ComposeResult:
        count = len(self.services)
        with Vertical(id="vault-target-card"):
            yield Label(
                tr("targets.platform_title", vault=visual_markup(self.vault_name)),
                id="vault-target-title",
            )
            yield Label(
                tr("targets.platform_help", count=count),
                id="vault-target-help",
            )
            yield OptionList(
                Option(
                    f"  [$accent]{tr('targets.all_platforms')}[/]  "
                    f"[$muted]{tr('targets.namespaces', count=count)}[/]"
                ),
                *[
                    Option(
                        f"  [$foreground]{visual_markup(label)}[/]  "
                        f"[$muted]{visual_markup(value)}[/]"
                    )
                    for value, label in self.services
                ],
                id="vault-target-list",
            )
            with Horizontal(id="vault-target-actions"):
                yield Button(phrase("Cancel"), id="vault-target-cancel")

    def on_mount(self) -> None:
        option_list = self.query_one("#vault-target-list", OptionList)
        option_list.highlighted = 0
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_index == 0:
            # An empty service value is the explicit all-platform choice. ``None``
            # remains reserved for cancelling the modal.
            self.dismiss("")
        elif 0 < event.option_index <= len(self.services):
            self.dismiss(self.services[event.option_index - 1][0])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()


__all__ = [
    "VaultSearchPicker",
    "VaultServicePicker",
    "VaultTargetResult",
    "VaultTargetScreen",
]

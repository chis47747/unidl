"""Which CDM answers, by the resolution being taken. Built by picking.

Reached from Settings -> "CDM rules by quality". The whole rule is assembled from
two lists - a threshold, then a device - because both halves are facts this app
already knows: the release heights it names files after, and the devices it can
actually see. Typing either one is how you get a rule pointing at a device that
does not exist, on the one screen where that mistake stays invisible until a
licence request fails.

The rows are shown in the order the rules are tried, not the order they were
added. "First match wins" is then something you read off the screen instead of
something you have to be told, and re-ordering by hand becomes a thing nobody has
to think about: the order is derived from what the rules say.

Nothing here is saved until you go back, and going back without changing anything
answers "nothing changed" rather than rewriting the same value - so opening this
screen to look at it does not count as an edit.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..core import cdmrules
from ..core.cdmrules import Rule
from ..core.i18n import tr
from ..core.settings import Option as SettingOption
from ..core.settings import Setting
from .bidi import visual_text
from .chrome import Chrome, KeyBar, refresh_locale_widgets

#: The row that adds one. Not a key on its own: an empty screen has to say what to
#: do next, and a key bar hint on a list with nothing in it is a hint about nothing.
ADD = "add"

#: Where the device column starts. Wide enough for "2160p and above" and "exactly
#: 1440p" with room to breathe.
CONDITION_WIDTH = 20


def _esc(text: object) -> str:
    """Neutralise square brackets: real device names are full of them."""
    return str(text).replace("\\", "\\\\").replace("[", r"\[")


class CdmRulesScreen(Screen[str | None]):
    """Returns the rules as canonical text, or ``None`` when nothing changed."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "noop", "Change the device", show=True),
        Binding("ctrl+n", "add_rule", "Add a rule", show=True),
        Binding("ctrl+x", "remove_rule", "Remove this rule", show=True),
    ]

    def action_noop(self) -> None:
        """Key-bar hint only; Enter is handled by the focused list."""

    def __init__(self, value: str = "") -> None:
        super().__init__()
        self._original = cdmrules.dump(cdmrules.parse(value))
        self._rules: list[Rule] = cdmrules.parse(value)
        #: one entry per list row: a rule, the add row, or None for a heading
        self._rows: list[Rule | str | None] = []

    def compose(self) -> ComposeResult:
        yield Chrome(can_go_back=True, show_settings=False)
        yield Static("", id="rules-head")
        yield Static("", id="rules-note")
        yield OptionList(id="rules-list")
        yield KeyBar(
            ("enter", "change the device"),
            ("^n", "add a rule"),
            ("^x", "remove this rule"),
            ("^b", "back, saving"),
            ("esc", "quit"),
        )

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#rules-list", OptionList).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.rebuild()

    # ------------------------------------------------------------------- devices
    def devices(self) -> list:
        """Every device this install can see, for the rows and for the warnings."""
        return cdmrules.known_devices(self.app.config)

    def _device(self, name: str):
        return next((d for d in self.devices() if d.name == name), None)

    # -------------------------------------------------------------------- render
    def rebuild(self) -> None:
        option_list = self.query_one("#rules-list", OptionList)
        previous = option_list.highlighted
        option_list.clear_options()
        self._rows = []
        palette = self.app.palette

        head = Text()
        head.append(tr("cdmrules.head"), style=f"bold {palette.accent}")
        head.append("  ·  ", style=palette.dim)
        if self._rules:
            head.append(
                tr("cdmrules.summary", count=len(self._rules)),
                style=palette.fg,
            )
        else:
            head.append(tr("cdmrules.none"), style=palette.muted)
        self.query_one("#rules-head", Static).update(head)

        devices = self.devices()
        problems = cdmrules.problems(
            self._rules,
            [device.name for device in devices],
            {device.name: device.system for device in devices},
        )
        note = Text()
        if problems:
            note.append("  ·  ".join(problems), style=palette.warn)
        elif not devices:
            note.append(tr("cdmrules.no_devices"), style=palette.warn)
        self.query_one("#rules-note", Static).update(note)

        if self._rules:
            self._rows.append(None)
            option_list.add_option(
                Option(
                    f"  [$dim]{tr('cdmrules.in_order')}[/]  [$gutter]{len(self._rules)}[/]",
                    disabled=True,
                )
            )
            for rule in cdmrules.order(self._rules):
                self._rows.append(rule)
                option_list.add_option(Option(self._row_markup(rule)))
        self._rows.append(None)
        option_list.add_option(Option(f"  [$dim]{tr('cdmrules.new')}[/]", disabled=True))
        self._rows.append(ADD)
        option_list.add_option(
            Option(
                f"    [$accent]{tr('cdmrules.add')}[/]  [$dim]{tr('cdmrules.add_hint')}[/]"
            )
        )

        if previous is not None and self._rows:
            option_list.highlighted = min(previous, len(self._rows) - 1)
        else:
            option_list.highlighted = 1 if len(self._rows) > 1 else 0

    def _row_markup(self, rule: Rule) -> str:
        """One rule: when it applies, which device answers, and where that is."""
        device = self._device(rule.device)
        where = ""
        if device is None:
            where = tr("cdmrules.not_found")
        elif device.is_remote:
            where = tr("cdmrules.remote", where=device.where)
        elif device.level:
            where = device.level
        condition = f"{rule.label():<{CONDITION_WIDTH}}"
        row = f"    {condition}[$accent]{_esc(visual_text(rule.device))}[/]"
        if where:
            row += f"  [$dim]{_esc(where)}[/]"
        return row

    def _selected(self) -> Rule | str | None:
        option_list = self.query_one("#rules-list", OptionList)
        index = option_list.highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index]

    # ---------------------------------------------------------------------- edit
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = self._selected()
        if chosen == ADD:
            self.action_add_rule()
        elif isinstance(chosen, Rule):
            self._pick_device(chosen)

    def action_add_rule(self) -> None:
        """A threshold first, then the device that answers it."""
        self._ask_condition()

    def action_remove_rule(self) -> None:
        chosen = self._selected()
        if not isinstance(chosen, Rule):
            self.notify(tr("cdmrules.highlight"), timeout=3)
            return
        self._rules = [rule for rule in self._rules if rule != chosen]
        self.rebuild()
        self.notify(tr("cdmrules.removed", label=chosen.label(), device=chosen.device), timeout=3)

    def _ask_condition(self) -> None:
        """The resolution half, as the same choice card every setting uses."""
        from .settings_screen import _ChoiceEditor

        spec = Setting(
            key="cdm_rule_condition",
            label=tr("cdmrules.condition"),
            kind="choice",
            options=[
                SettingOption(
                    cdmrules.condition_text(op, height),
                    cdmrules.condition_label(op, height),
                )
                for op, height in cdmrules.conditions()
            ],
            help=tr("cdmrules.condition_help"),
        )

        def _done(value) -> None:
            if not value:
                return
            rule = cdmrules.parse_line(f"{value} placeholder")
            if rule is None:
                return
            self._pick_device(rule, replacing=None)

        self.app.push_screen(_ChoiceEditor(spec, None), _done)

    def _pick_device(self, rule: Rule, replacing: Rule | None = ...) -> None:
        """The device half, in the real picker - the one with the search box.

        ``replacing`` defaults to ``rule`` itself, which is the "change the device
        on this rule" case; adding passes ``None``, because there is nothing to
        take out of the list yet.
        """
        from .cdm_screen import CdmScreen

        target = rule if replacing is ... else replacing
        active = str(self.app.globals.get("drm_system", "") or "widevine")

        def _done(name) -> None:
            if not name:
                return
            replacement = Rule(rule.op, rule.height, str(name))
            # One rule per comparison *per DRM system*: a second rule for the same
            # system at the same threshold could never be reached, while one for the
            # other system is the whole point - "the L1 for HD, that PlayReady
            # endpoint for HD" is two rules that never compete.
            systems = cdmrules.device_systems(self.app.config)
            taken = cdmrules.slot(replacement, systems)
            kept = [
                existing
                for existing in self._rules
                if existing != target and cdmrules.slot(existing, systems) != taken
            ]
            self._rules = [*kept, replacement]
            self.rebuild()
            self.notify(
                tr("cdmrules.changed", label=replacement.label(), device=replacement.device),
                timeout=3,
            )

        self.app.push_screen(
            CdmScreen(
                current=rule.device,
                system=active,
                note=tr("cdmrules.for", label=rule.label()),
            ),
            _done,
        )

    # ---------------------------------------------------------------------- exit
    def go_back(self) -> bool:
        """Answer with the rules, or with "nothing changed" if they are the same."""
        text = cdmrules.dump(self._rules)
        self.dismiss(text if text != self._original else None)
        return True


__all__ = ["ADD", "CdmRulesScreen"]

"""Settings panel, reachable from anywhere with ``s``.

Three sections, in order of how specific they are:

1. **Global** - what happens on pick, command export, debug. Same everywhere.
2. **<Service>** - that service's own vocabulary: Amazon's profile strings,
   BBC's resolution names, Paramount's platform / region / market. Only shown
   when a service is active, and services can declare as many of these as they
   need; there is no fixed set.
3. **Tracks and output** - the shared quality vocabulary, applied against the
   real ladder after the manifest is parsed.

Edits persist immediately, so they take effect on the next request without a
restart. Settings that invalidate a login are marked and drop the session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from ..core import cdmrules, naming, template, vaults
from ..core.i18n import (
    cell_width,
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
from .bidi import visual_markup
from .chrome import Chrome, KeyBar, StatusChip
from .input import ClipboardInput as Input


class _Editor(ModalScreen[Any]):
    """Base for the two value editors.

    These carry the chrome bar and the key bar like every other screen. Without
    them, opening a setting made Back and Quit vanish from the top-left - the
    shortcuts still worked, but a control you cannot see is not a control, and
    the frame is supposed to be in the same place everywhere.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(self, setting: Setting, current: Any):
        super().__init__()
        self.setting = setting
        self.current = current
        # opaque: a see-through modal shows the settings screen's own chrome and
        # key bar behind its own, so you get two of each and neither is
        # obviously the live one
        self.add_class("editor")

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        # the card is centred inside this, so the chrome and key bar stay where
        # they are on every other screen instead of floating with it
        with Vertical(id="modal-body"):
            with Vertical(id="modal-card"):
                yield from self.compose_title()
                help_text = setting_help(self.setting)
                if help_text:
                    yield Label(help_text, classes="ask-hint")
                yield from self.compose_editor()
        yield KeyBar(*self.key_hints())

    def compose_title(self) -> ComposeResult:
        yield Label(setting_label(self.setting), classes="ask-title")

    def compose_editor(self) -> ComposeResult:
        yield from ()

    def key_hints(self) -> list[tuple[str, str]]:
        return [("enter", "confirm"), ("^b", "cancel"), ("esc", "cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
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

    def key_hints(self) -> list[tuple[str, str]]:
        return [("enter", "use this value"), ("↑↓", "move"), ("^b", "cancel"), ("esc", "cancel")]

    def on_mount(self) -> None:
        option_list = self.query_one(OptionList)
        for index, option in enumerate(self.setting.options):
            if option.value == self.current:
                option_list.highlighted = index
                break
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.setting.options[int(event.option.id or 0)].value)


class _MultiChoiceEditor(_Editor):
    """Checkbox editor for service API profiles/resolutions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("space", "toggle", "Toggle", show=False),
        Binding("a", "apply", "Apply", show=False),
    ]

    def __init__(self, setting: Setting, current: Any):
        super().__init__(setting, current)
        self.selected = list(setting.coerce(current))

    def compose_title(self) -> ComposeResult:
        with Horizontal(classes="multi-setting-title-row"):
            yield Label(setting_label(self.setting), classes="ask-title")
            confirm = StatusChip(
                "apply",
                id="multi-setting-confirm",
                classes="multi-setting-confirm",
            )
            confirm.update(f"✓ {tr('common.confirm', default='Confirm')}")
            yield confirm

    def compose_editor(self) -> ComposeResult:
        yield OptionList(id="multi-setting-options")

    def key_hints(self) -> list[tuple[str, str]]:
        return [
            ("enter/space", "toggle"),
            ("a", "apply"),
            ("^b", "cancel"),
            ("esc", "cancel"),
        ]

    def on_mount(self) -> None:
        self._rebuild_options()
        self.query_one("#multi-setting-options", OptionList).focus()

    def _rebuild_options(self) -> None:
        options = self.query_one("#multi-setting-options", OptionList)
        previous = options.highlighted
        options.clear_options()
        options.add_option(
            Option(
                f"  [$accent]{tr('settings.use_n', count=len(self.selected))}[/]",
                id="apply",
            )
        )
        for index, item in enumerate(self.setting.options):
            checked = item.value in self.selected
            mark = "[x]" if checked else "[ ]"
            role = "accent" if checked else "muted"
            options.add_option(
                Option(
                    f"  [${role}]{mark}[/]  {visual_markup(item.display())}",
                    id=f"value-{index}",
                )
            )
        options.highlighted = min(previous or 0, len(self.setting.options))

    def _toggle_index(self, index: int | None) -> None:
        if index is None or index <= 0:
            return
        option = self.setting.options[index - 1]
        if option.value in self.selected:
            self.selected.remove(option.value)
        else:
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


class _TextEditor(_Editor):
    def compose_editor(self) -> ComposeResult:
        yield Input(
            value="" if self.current is None else str(self.current),
            placeholder=tr("settings.placeholder"),
            classes="ask-input",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)


#: Which settings are name templates, and which fields each may use. A table rather
#: than a flag on Setting: it is two facts about three settings, and the alternative
#: was a validator hook on every setting there has ever been.
TEMPLATE_FIELDS = {
    "name_template_episode": naming.TITLE_FIELDS,
    "name_template_movie": naming.TITLE_FIELDS,
    "release_template": naming.RELEASE_FIELDS,
}


#: The label column. Wide enough for the longest label the app declares - "Send
#: downloads through the proxy" is thirty-two characters - because the value
#: starting at the same column on every row is what makes the list readable as two
#: columns rather than as sentences.
LABEL_WIDTH = 34

#: Never fewer than this many spaces between a label and its value. A label longer
#: than the column overruns it rather than being cut, and without a floor the two
#: ran together: "Send downloads through the proxyoff" was one word.
LABEL_GAP = 2


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

    def __init__(self, service: Service | None, globals_scope: Settings):
        super().__init__()
        self.service = service
        self.globals = globals_scope
        #: the settings scope edits are written through
        self.scope = service.settings if service is not None else globals_scope
        self._rows: list[tuple[Setting, Settings] | None] = []
        #: whether the list was last built with the blank rows between settings
        self._roomy = True

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
            title = f"{tr('settings.title')}  [$dim]{self.service.NAME}[/]"
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
            title = f"{tr('settings.title')}  [$dim]{self.service.NAME}[/]"
        found = self.query("#masthead")
        if found:
            found.first(Static).update(title)
        self.rebuild()

    def set_hint(self, text: str) -> None:
        note = f"[$dim]{visual_markup(text)}[/]" if text else ""
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
        """Rebuild when the window crosses into or out of the roomy size.

        The blank rows between settings are options, not padding, so they cannot be
        turned off by the stylesheet the way the rest of the spacing is - which
        means this screen has to be told the window changed.
        """
        roomy = not self.app.has_class("short")
        if roomy is not self._roomy:
            self._roomy = roomy
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
            visible_specs = [spec for spec in specs if spec.visible]
            if not visible_specs:
                return
            self._rows.append(None)
            suffix = f"  [$gutter]{note}[/]" if note else ""
            option_list.add_option(Option(f"  [$dim]{name.upper()}[/]{suffix}", disabled=True))
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
            add_section(self.service.NAME, scope.service_specs(), scope, note=service_note)
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
        if found is not None:
            option_list.highlighted = found
        elif settings_rows:
            option_list.highlighted = settings_rows[0]

    def _row_markup(self, spec: Setting, scope: Settings) -> str:
        if spec.picker in {
            "resources",
            "storage",
            "proxy_manager",
            "justwatch",
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
        # Padded to a column, but never joined to the next one: a label longer than
        # the column gets the minimum gap instead of being run into its own value.
        label_text = setting_label(spec)
        label = visual_markup(label_text)
        gap = " " * max(LABEL_GAP, LABEL_WIDTH - cell_width(label_text))
        row = f"    {label}{gap}[$accent]{visual_markup(value)}[/]"
        if spec.resets_session:
            row += f"  [$dim]{tr('settings.signs_out')}[/]"
        return row

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
            problems = _template_problems(spec, value)
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

        if spec.picker in {"download_behavior", "interface"}:
            from .settings_group import SettingsGroupScreen

            groups = {
                "download_behavior": (
                    "settings.group.download",
                    "settings.group.download_help",
                    (
                        "after_resolve",
                        "live_record",
                        "license_after_tracks",
                        "confirm_batch",
                        "retries",
                        "http_timeout",
                        "max_speed",
                        "muxer",
                        "segment_downloader",
                        "resume_parts",
                        "check_segments_count",
                        "keep_temp",
                        "delete_temp_after_done",
                        "auto_subtitle_fix",
                        "live_real_time_merge",
                        "live_keep_segments",
                        "live_pipe_mux",
                    ),
                ),
                "interface": (
                    "settings.group.interface",
                    "settings.group.interface_help",
                    ("theme", "interface_locale", "debug"),
                ),
            }
            title, description, keys = groups[spec.picker]
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

        if spec.kind == "multi" and spec.options:
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
            self._apply(spec, scope, spec.default)

    def go_back(self) -> bool:
        # dismiss, not pop: Textual runs the callback this screen was pushed with
        # on dismiss and discards it on pop. ^b is how settings are normally
        # closed, and popping meant the theme was never re-applied, the login
        # hints were never invalidated, and an open session's screens were never
        # refreshed - everything `action_global_settings` does on the way out.
        self.dismiss(None)
        return True

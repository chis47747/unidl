"""What is installed and what is missing, before it matters.

Reached by clicking ``ready`` on the main screen. Everything on it was already
known - a service declares the tools it needs, the resolver finds them or does
not, the DRM registry knows which libraries imported - and none of it was visible
until something failed halfway through a download. This is that report, up front.

Read-only on purpose. It says which command installs the missing thing and does
not run it: a screen that installs software is a screen that has to be trusted,
and this one only has to be believed.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from .. import __version__
from ..core import readiness
from ..core.i18n import cell_width, get_translator, phrase, tr
from ..core.redact import redact
from .bidi import rtl_terminal_mode, visual_text
from .chrome import Chrome, CloseMark, KeyBar, StatusChip, fit_modal_card, refresh_locale_widgets
from .poster import DEFAULT_POSTER_PREVIEW, detect_terminal_poster_capability, normalize_poster_preview
from .terminal_progress import supports_osc_progress

#: state -> (glyph, palette role).  Core runtime gaps are red, global DRM setup
#: gaps are yellow, and service/Hybrid enhancements are blue.
MARK_OK = ("✓", "ok")
MARK_BLOCKING = ("✗", "error")
MARK_GLOBAL = ("·", "warn")
MARK_ENHANCEMENT = ("+", "manifest")
# Compatibility name for extensions that imported the old two-state marker.
# Optional now means an enhancement (blue); global capability gaps have their
# explicit ``MARK_GLOBAL`` marker.
MARK_OPTIONAL = MARK_ENHANCEMENT


#: The label column. Wide enough for platform labels and every asset name
#: bar a couple, which overrun it rather than being cut.
LABEL_WIDTH = 30

#: A version probe can answer with a paragraph and a build string; this is a table.
DETAIL_LIMIT = 52


def _first_line(text: str) -> str:
    """The first line of what a tool said about itself, shortened to fit."""
    first = visual_text(text).splitlines()[0] if text else ""
    return first if len(first) <= DETAIL_LIMIT else f"{first[: DETAIL_LIMIT - 1]}…"


_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+\S+")


def redact_diagnostics_text(text: str) -> str:
    """Replace local home directory paths with ``~`` and scrub credentials."""
    cleaned = _BEARER_RE.sub(r"\1 ***", redact(str(text or "")))
    homes: list[str] = []
    try:
        home = Path.home()
        homes.append(str(home))
        resolved = str(home.resolve())
        if resolved not in homes:
            homes.append(resolved)
    except Exception:
        pass
    for raw_home in sorted(homes, key=len, reverse=True):
        if raw_home and len(raw_home) > 1:
            cleaned = cleaned.replace(raw_home, "~")
            posix_home = raw_home.replace("\\", "/")
            if posix_home != raw_home:
                cleaned = cleaned.replace(posix_home, "~")
    return cleaned


def format_diagnostics_report(app: object, report: readiness.Report) -> str:
    """Build a redacted, issue-ready environment and readiness summary."""
    term = os.environ.get("TERM") or "unknown"
    term_prog = os.environ.get("TERM_PROGRAM") or "unknown"
    locale = get_translator().locale
    theme = getattr(app, "theme_preference", "dark")
    accent = getattr(app, "accent_preference", "violet")
    semantic = getattr(app, "semantic_colors_preference", "default")
    density = getattr(app, "density_preference", "auto")
    globals_scope = getattr(app, "globals", None)
    poster_mode = normalize_poster_preview(
        globals_scope.get("poster_preview", DEFAULT_POSTER_PREVIEW)
        if hasattr(globals_scope, "get")
        else DEFAULT_POSTER_PREVIEW
    )
    poster_cap, _poster_term = detect_terminal_poster_capability()
    drm_system = (
        str(globals_scope.get("drm_system", "widevine") or "widevine")
        if hasattr(globals_scope, "get")
        else "unknown"
    )
    config = getattr(app, "config", None)
    device_kind = "none"
    if config is not None and hasattr(config, "device_name_for"):
        try:
            dev_name = config.device_name_for("") or ""
            if dev_name:
                remote = config.remote_cdm(dev_name) if hasattr(config, "remote_cdm") else None
                device_kind = "remote" if remote is not None else "local"
        except Exception:
            device_kind = "unknown"

    lines: list[str] = [
        "UniDL Diagnostics",
        "=================",
        f"Version:   {__version__}",
        f"OS:        {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python:    {platform.python_version()}",
        (
            f"Terminal:  TERM={term} · TERM_PROGRAM={term_prog} · "
            f"BiDi={rtl_terminal_mode()} · OSC9;4={'yes' if supports_osc_progress() else 'no'}"
        ),
        (
            f"Interface: locale={locale} · theme={theme} · accent={accent} · "
            f"semantic={semantic} · density={density} · posters={poster_mode}({poster_cap})"
        ),
        f"DRM:       system={drm_system} · device={device_kind}",
    ]
    try:
        from ..core.binaries import collect_tool_versions

        tool_versions = collect_tool_versions()
        if tool_versions:
            lines.append("Tools:     " + " · ".join(f"{k}={v}" for k, v in sorted(tool_versions.items())))
    except Exception:
        pass
    lines.append("")
    for group in readiness.GROUPS:
        items = report.group(group)
        if not items:
            continue
        lines.append(f"{tr(f'ready.group.{group}', default=readiness.GROUP_LABEL[group])}:")
        for item in items:
            glyph, _ = mark_for(item)
            first = item.detail.splitlines()[0] if item.detail else ""
            lines.append(f"  {glyph} {item.label}  {redact_diagnostics_text(first)}")
            if not item.ok and item.hint:
                lines.append(f"      {redact_diagnostics_text(item.hint)}")
    return "\n".join(lines)


def mark_for(item: readiness.Item) -> tuple[str, str]:
    if item.ok:
        return MARK_OK
    if item.blocking:
        return MARK_BLOCKING
    if item.level == readiness.ENHANCEMENT:
        return MARK_ENHANCEMENT
    return MARK_GLOBAL


class ReadinessScreen(Screen[None]):
    """One page: DRM systems, tools, assets, folders."""

    BINDINGS = [
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("slash", "app.global_search", "Search", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+r", "reload", "Check again", show=True),
        Binding("i", "setup_tools", "Install tools", show=True),
        Binding("ctrl+y", "copy_all", "Copy the report", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._report: readiness.Report | None = None
        self._survey_generation = 0
        self._checking = False

    def compose(self) -> ComposeResult:
        yield Chrome(can_go_back=True)
        with Horizontal(id="ready-bar"):
            yield Static("", id="ready-head")
            yield StatusChip("setup_tools", id="ready-setup")
            yield StatusChip("copy_all", id="ready-copy")
        yield VerticalScroll(id="ready-body")
        yield KeyBar(("^r", "check again"), ("i", tr("ready.setup.key", default="install tools")), ("^y", "copy the report"), ("^b", "back"))

    def on_mount(self) -> None:
        signal = getattr(self.app, "theme_changed_signal", None)
        if signal is not None:
            signal.subscribe(self, self._on_theme_changed)
        self.query_one("#ready-body", VerticalScroll).focus()
        self.build()

    def on_unmount(self) -> None:
        signal = getattr(self.app, "theme_changed_signal", None)
        if signal is not None:
            signal.unsubscribe(self)
        self._survey_generation += 1
        self.workers.cancel_group(self, "readiness-survey")

    def _on_theme_changed(self, _theme: object = None) -> None:
        if self.is_attached and self._report is not None:
            self._render_report(self._report)

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        if self._report is not None:
            self._render_report(self._report)
        else:
            self.build()

    # ------------------------------------------------------------------ content
    def action_reload(self) -> None:
        # the one thing that really goes and looks again: everything else is
        # answered from what the last survey found
        if self._checking:
            return
        self.build(fresh=True, notify=True)

    def build(self, *, fresh: bool = False, notify: bool = False) -> None:
        """Survey tools off the UI thread, then paint."""
        self._survey_generation += 1
        generation = self._survey_generation
        self._checking = True
        if self._report is None or fresh:
            palette = self.app.palette
            head = Text()
            head.append(tr("ready.head"), style=f"bold {palette.accent}")
            head.append("  ·  ", style=palette.dim)
            head.append("…", style=palette.muted)
            self.query_one("#ready-head", Static).update(head)
            for chip_id in ("#ready-setup", "#ready-copy"):
                chips = self.query(chip_id)
                if chips:
                    chips.first(StatusChip).display = False
        self._run_survey(fresh=fresh, notify=notify, generation=generation)

    @work(exclusive=True, group="readiness-survey")
    async def _run_survey(self, *, fresh: bool, notify: bool, generation: int) -> None:
        try:
            report = await asyncio.to_thread(
                readiness.survey,
                self.app.config,
                self.app.registry,
                fresh=fresh,
                settings=self.app.globals,
            )
        finally:
            if generation == self._survey_generation:
                self._checking = False
        if not self.is_attached or generation != self._survey_generation:
            return
        self._report = report
        self._render_report(report)
        if notify:
            self.notify(tr("ready.checked"), timeout=2)

    def _render_report(self, report: readiness.Report) -> None:
        palette = self.app.palette
        head = Text()
        head.append(tr("ready.head"), style=f"bold {palette.accent}")
        head.append("  ·  ", style=palette.dim)
        if report.status == "missing":
            head.append(tr("ready.missing", count=len(report.blocking)), style=palette.error)
        elif report.status == "global":
            head.append(tr("ready.global_missing", count=len(report.global_missing)), style=palette.warn)
        elif report.status == "partial":
            head.append(tr("ready.partial", count=len(report.enhancement_missing)), style=palette.manifest)
        else:
            head.append(tr("ready.ok"), style=palette.ok)
        self.query_one("#ready-head", Static).update(head)
        setup_chips = self.query("#ready-setup")
        if setup_chips:
            chip = setup_chips.first(StatusChip)
            chip.update(tr("ready.setup.button", default="Install tools"))
            chip.display = True
        copy_chips = self.query("#ready-copy")
        if copy_chips:
            chip = copy_chips.first(StatusChip)
            chip.update(tr("ready.copy_diagnostics"))
            chip.display = True

        body = self.query_one("#ready-body", VerticalScroll)
        saved_scroll_y = body.scroll_y
        body.remove_children()
        rows: list[Static] = []
        for group in readiness.GROUPS:
            items = report.group(group)
            if not items:
                continue
            heading = Text()
            heading.append(
                tr(f"ready.group.{group}", default=readiness.GROUP_LABEL[group]).upper(),
                style=palette.muted,
            )
            heading.append(f"   {len(items)}", style=palette.gutter)
            rows.append(Static(heading, classes="ready-group"))
            rows += [Static(self._row(item), classes="ready-row") for item in items]
        body.mount_all(rows)
        if saved_scroll_y > 0:
            self.call_after_refresh(lambda: body.scroll_to(y=saved_scroll_y, animate=False))

    def _row(self, item: readiness.Item) -> Text:
        """One item: what it is, what was found, and what to do if it was not."""
        palette = self.app.palette
        glyph, role = mark_for(item)
        line = Text()
        line.append(f"{glyph}  ", style=getattr(palette, role, palette.fg))
        # padded to a column, but never joined to the next one: a label longer than
        # the column still gets its two spaces rather than running into the detail
        label = visual_text(item.label)
        pad = " " * max(0, LABEL_WIDTH - cell_width(label))
        line.append(f"{label}{pad}  ", style=palette.fg if item.ok else palette.fg2)
        detail = visual_text(item.detail) if item.group == "folders" else _first_line(item.detail)
        line.append(detail, style=palette.dim if item.ok else palette.fg2)
        if not item.ok:
            if item.hint:
                line.append(f"\n     {visual_text(item.hint)}", style=palette.accent)
            if item.without:
                line.append(
                    f"\n     {tr('ready.without', text=visual_text(item.without))}",
                    style=palette.muted,
                )
        if item.needed_by and not item.ok:
            line.append(
                f"\n     {tr('ready.asked', names=', '.join(item.needed_by))}",
                style=palette.muted,
            )
        return line

    # ------------------------------------------------------------------- copying
    def action_copy_all(self) -> None:
        """Copy a redacted environment and readiness diagnostics report."""
        report = self._report
        if report is None:
            return
        text = format_diagnostics_report(getattr(self, "app", None), report)
        try:
            if self.app.copy_text(text) is False:
                return
        except Exception:  # noqa: BLE001 - a terminal that refuses the clipboard
            self.notify(text, title=tr("ready.title"), timeout=12)
            return
        self.notify(tr("ready.copied", count=len(report.items)), timeout=3)

    def action_setup_tools(self) -> None:
        """Open explicit external-tool dependency setup modal."""
        self.app.push_screen(
            _ToolSetupModal(),
            lambda installed: self.build(fresh=True, notify=True) if installed else None,
        )


class _ToolSetupModal(ModalScreen[bool]):
    """Explicit user-confirmed installer for missing external CLI dependencies."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Close", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._missing: list[object] = []
        self._installed_any = False
        self._inspect_generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-card"):
            with Horizontal(classes="modal-head"):
                yield Label(tr("ready.setup.title", default="External tool setup"), classes="modal-card-title")
                yield CloseMark(id="modal-close")
            yield Label(
                tr("ready.setup.help", default="Select a missing tool to install via your system package manager, or run the shown command manually."),
                classes="modal-card-help",
            )
            yield OptionList(id="ready-setup-list")
            with Horizontal(classes="modal-card-actions"):
                yield Button(tr("ready.setup.install", default="Install selected"), variant="primary", id="ready-setup-install")
                yield Button(phrase("Close"), id="ready-setup-close")

    def on_mount(self) -> None:
        self._start_inspect()
        fit_modal_card(self, "#modal-card", "#ready-setup-list")
        self.query_one("#ready-setup-list", OptionList).focus()

    def on_unmount(self) -> None:
        self._inspect_generation += 1
        self.workers.cancel_group(self, "tool-setup-inspect")

    def _start_inspect(self) -> None:
        self._inspect_generation += 1
        generation = self._inspect_generation
        self._missing = []
        option_list = self.query_one("#ready-setup-list", OptionList)
        option_list.clear_options()
        option_list.add_option(
            Option(tr("ready.setup.loading", default="Checking installed tools…"), disabled=True)
        )
        self.query_one("#ready-setup-install", Button).disabled = True
        self._inspect_worker(generation)

    @work(thread=True, exclusive=True, group="tool-setup-inspect")
    def _inspect_worker(self, generation: int) -> None:
        from ..core.setup_tools import inspect_tools

        statuses = inspect_tools(include_registered=True, use_cache=False)
        try:
            self.app.call_from_thread(self._apply_statuses, generation, statuses)
        except RuntimeError:
            pass

    def _apply_statuses(self, generation: int, statuses: list[object]) -> None:
        if not self.is_attached or generation != self._inspect_generation:
            return
        option_list = self.query_one("#ready-setup-list", OptionList)
        option_list.clear_options()
        self._missing = [st for st in statuses if not st.installed]
        if not self._missing:
            option_list.add_option(Option(tr("ready.setup.all_ok", default="All catalogued CLI tools are installed."), disabled=True))
            self.query_one("#ready-setup-install", Button).disabled = True
            fit_modal_card(self, "#modal-card", "#ready-setup-list")
            return
        self.query_one("#ready-setup-install", Button).disabled = False
        for st in self._missing:
            option_list.add_option(Option(f"{st.spec.name:<16}  {st.install_command}", id=st.spec.name))
        option_list.highlighted = 0
        fit_modal_card(self, "#modal-card", "#ready-setup-list")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "ready-setup-install":
            self._trigger_install()
        else:
            self.action_close()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._trigger_install()

    def _trigger_install(self) -> None:
        option_list = self.query_one("#ready-setup-list", OptionList)
        idx = option_list.highlighted
        if idx is None or idx >= len(self._missing):
            return
        target = self._missing[idx].spec.name
        self.notify(tr("ready.setup.running", tool=target, default=f"Installing {target}…"), timeout=6)
        self._run_install(target)

    @work(thread=True, exclusive=True)
    def _run_install(self, tool_name: str) -> None:
        from ..core.setup_tools import install_tool

        result = install_tool(tool_name)
        if result.ok:
            self._installed_any = True
        try:
            self.app.call_from_thread(self._after_install, result)
        except RuntimeError:
            pass

    def _after_install(self, result) -> None:
        if not self.is_attached:
            return
        self.notify(
            f"{result.tool}: {result.detail}",
            severity="information" if result.ok else "warning",
            timeout=8,
        )
        self._start_inspect()

    def action_close(self) -> None:
        self.dismiss(self._installed_any)

    def go_back(self) -> bool:
        self.action_close()
        return True


__all__ = [
    "ReadinessScreen",
    "format_diagnostics_report",
    "mark_for",
    "redact_diagnostics_text",
]

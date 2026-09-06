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

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ..core import readiness
from ..core.i18n import tr
from .bidi import visual_text
from .chrome import Chrome, KeyBar, refresh_locale_widgets

#: state -> (glyph, palette role). Three states, not two: a missing optional tool
#: costs a feature, a missing required one stops a service, and drawing both in red
#: makes the report look broken when it is merely incomplete.
MARK_OK = ("✓", "ok")
MARK_BLOCKING = ("✗", "error")
MARK_OPTIONAL = ("·", "warn")


#: The label column. Wide enough for platform labels and every asset name
#: bar a couple, which overrun it rather than being cut.
LABEL_WIDTH = 30

#: A version probe can answer with a paragraph and a build string; this is a table.
DETAIL_LIMIT = 52


def _first_line(text: str) -> str:
    """The first line of what a tool said about itself, shortened to fit."""
    first = visual_text(text).splitlines()[0] if text else ""
    return first if len(first) <= DETAIL_LIMIT else f"{first[: DETAIL_LIMIT - 1]}…"


def mark_for(item: readiness.Item) -> tuple[str, str]:
    if item.ok:
        return MARK_OK
    return MARK_BLOCKING if item.required else MARK_OPTIONAL


class ReadinessScreen(Screen[None]):
    """One page: DRM systems, tools, assets, folders."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+r", "reload", "Check again", show=True),
        Binding("ctrl+y", "copy_all", "Copy the report", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._report: readiness.Report | None = None

    def compose(self) -> ComposeResult:
        yield Chrome(can_go_back=True)
        yield Static("", id="ready-head")
        yield VerticalScroll(id="ready-body")
        yield KeyBar(("^r", "check again"), ("^y", "copy the report"), ("^b", "back"))

    def on_mount(self) -> None:
        self.build()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.build()

    # ------------------------------------------------------------------ content
    def action_reload(self) -> None:
        # the one thing that really goes and looks again: everything else is
        # answered from what the last survey found
        self.build(fresh=True)
        self.notify(tr("ready.checked"), timeout=2)

    def build(self, *, fresh: bool = False) -> None:
        """Survey, then draw. Both here: the survey is what takes the time."""
        palette = self.app.palette
        report = readiness.survey(self.app.config, self.app.registry, fresh=fresh)
        self._report = report

        head = Text()
        head.append(tr("ready.head"), style=f"bold {palette.accent}")
        head.append("  ·  ", style=palette.dim)
        blocking = report.blocking
        if blocking:
            head.append(tr("ready.missing", count=len(blocking)), style=palette.error)
        else:
            optional = [item for item in report.missing if item.group != "folders"]
            extra = tr("ready.optional", count=len(optional)) if optional else ""
            head.append(
                tr("ready.ok") + extra,
                style=palette.ok if not optional else palette.fg,
            )
        self.query_one("#ready-head", Static).update(head)

        body = self.query_one("#ready-body", VerticalScroll)
        body.remove_children()
        rows: list[Static] = []
        for group in readiness.GROUPS:
            items = report.group(group)
            if not items:
                continue
            heading = Text()
            heading.append(readiness.GROUP_LABEL[group].upper(), style=palette.muted)
            heading.append(f"   {len(items)}", style=palette.gutter)
            rows.append(Static(heading, classes="ready-group"))
            rows += [Static(self._row(item), classes="ready-row") for item in items]
        body.mount_all(rows)

    def _row(self, item: readiness.Item) -> Text:
        """One item: what it is, what was found, and what to do if it was not."""
        palette = self.app.palette
        glyph, role = mark_for(item)
        line = Text()
        line.append(f"{glyph}  ", style=getattr(palette, role, palette.fg))
        # padded to a column, but never joined to the next one: a label longer than
        # the column still gets its two spaces rather than running into the detail
        label = visual_text(item.label)
        line.append(f"{label:<{LABEL_WIDTH}}  ", style=palette.fg if item.ok else palette.fg2)
        line.append(_first_line(item.detail), style=palette.dim if item.ok else palette.fg2)
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
        """The whole report as text, for pasting where it can be answered."""
        report = self._report
        if report is None:
            return
        lines: list[str] = []
        for group in readiness.GROUPS:
            items = report.group(group)
            if not items:
                continue
            lines.append(f"{readiness.GROUP_LABEL[group]}:")
            for item in items:
                glyph, _ = mark_for(item)
                # the whole first line here, not the shortened one: this is going
                # into a bug report, where the build string is the useful part
                first = item.detail.splitlines()[0] if item.detail else ""
                lines.append(f"  {glyph} {item.label}  {first}")
                if not item.ok and item.hint:
                    lines.append(f"      {item.hint}")
        text = "\n".join(lines)
        try:
            self.app.copy_text(text)
        except Exception:  # noqa: BLE001 - a terminal that refuses the clipboard
            self.notify(text, title=tr("ready.title"), timeout=12)
            return
        self.notify(tr("ready.copied", count=len(report.items)), timeout=3)


__all__ = ["ReadinessScreen", "mark_for"]

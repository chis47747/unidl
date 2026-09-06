"""One log line, as data rather than as pre-coloured text.

Every line the session writes - a step it is taking, a value it resolved, a
failure - goes through here, for two reasons.

**They line up.** Each line begins with the same three columns: a state dot and
two spaces. Before this, plain messages carried an ASCII prefix (``+``, ``x``,
``!`` or three spaces), field rows carried three spaces and their own label
column, and a couple of places wrote straight into the log with no prefix at all -
so the command text started hard against the left edge while everything around it
sat three columns in. One gutter, one width, no exceptions.

**They can be re-rendered.** A line is kept as its parts, not as finished text, so
the same line can be drawn again in a different style. That is what lets the
newest line pulse while the work it describes is still running, and it means a
line's colours come from the palette in force when it is drawn rather than the one
in force when it happened.

The dot says what state the line is in; the words are left in the ordinary text
colour except for a failure, which is worth saying twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.table import Table
from rich.text import Text

from .bidi import visual_text

#: The state dot. One cell wide in Rich's own measurement, which is what the
#: layout is built on.
DOT = "●"

#: Dot plus two spaces. Everything in the log starts at this column, including a
#: field row's label and a bare artefact like a command.
GUTTER_WIDTH = 3

#: How wide a field row's label column is, so ``title``, ``manifest`` and ``key``
#: line their values up with each other.
LABEL_WIDTH = 9

#: state -> (palette role for the dot, palette role for the words). The words stay
#: in the ordinary colour almost everywhere: colouring every line by state turns
#: the log into a rainbow and stops the exceptional lines standing out. A failure
#: is the exception, because a red line is the one thing worth finding by eye.
STATE_STYLE = {
    #: work in progress. The theme's own colour, and the only state that pulses.
    "run": ("accent", "fg"),
    "ok": ("ok", "fg"),
    "warning": ("warn", "fg"),
    "error": ("error", "error"),
    #: plain information, and anything a caller did not name
    "info": ("fg", "fg"),
}

#: What older level names mean, so a caller that says "warn" is not silently
#: treated as unknown.
STATE_ALIASES = {"warn": "warning", "err": "error", "fail": "error", "": "info"}

#: Only this state pulses; every other line is something that has already happened.
LIVE_STATE = "run"

_OSC_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_C1_OSC_ESCAPE = re.compile(r"\x9d[^\x07\x9c]*(?:\x07|\x9c)")
_CSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_C1_CSI_ESCAPE = re.compile(r"\x9b[0-?]*[ -/]*[@-~]")
_ESCAPE = re.compile(r"\x1b[ -/]*[@-~]")


def safe_terminal_text(value: object) -> str:
    """Printable log text with terminal styles, cursor controls and bells removed."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _OSC_ESCAPE.sub("", text)
    text = _C1_OSC_ESCAPE.sub("", text)
    text = _CSI_ESCAPE.sub("", text)
    text = _C1_CSI_ESCAPE.sub("", text)
    text = _ESCAPE.sub("", text)
    return "".join(
        character
        for character in text
        if character in {"\n", "\t"}
        or (32 <= ord(character) < 127)
        or ord(character) >= 160
    )


def normalise(state: str) -> str:
    """A caller's level name as one of :data:`STATE_STYLE`'s keys."""
    name = str(state or "").strip().lower()
    name = STATE_ALIASES.get(name, name)
    return name if name in STATE_STYLE else "info"


@dataclass
class LogLine:
    """A line of the session log, keeping what it is made of.

    ``label`` and ``role`` are what make a field row: ``title``, ``manifest`` and
    ``key`` are values worth telling apart by colour, and they keep the colour they
    have always had. Everything else is a message.
    """

    body: str = ""
    state: str = "info"
    #: a field row's label, padded into its own column before the value
    label: str = ""
    #: palette role for the value of a field row
    role: str = ""
    #: a spacer between runs. No dot: there is no state to report, and a column of
    #: dots down an empty line reads as content that failed to arrive.
    blank: bool = False

    def __post_init__(self) -> None:
        self.body = safe_terminal_text(self.body)
        self.label = safe_terminal_text(self.label).replace("\n", " ")

    @property
    def plain(self) -> str:
        """The line as text, which is what a selection or a check reads."""
        if self.blank:
            return ""
        parts = [f"{DOT}  "]
        if self.label:
            parts.append(f"{self.label:<{LABEL_WIDTH}}")
        parts.append(self.body)
        return "".join(parts)

    def __str__(self) -> str:
        return self.plain

    @property
    def live(self) -> bool:
        """True for a line whose work is still going on, so its dot should pulse."""
        return not self.blank and normalise(self.state) == LIVE_STATE

    def render(self, palette, *, dim: bool = False):
        """The line as a renderable, gutter and all.

        A two-column grid rather than one run of text, so that a line too long for
        the pane wraps *under its own first character* instead of back to the
        gutter. A manifest URL and a UniDL command are both longer than any
        terminal, and this is where they are read.

        ``dim`` draws the dot in the faded half of a pulse; the words never change
        brightness, because text that does is much harder to read than a dot that
        does.
        """
        if self.blank:
            return Text("")
        dot_role, body_role = STATE_STYLE[normalise(self.state)]
        row = Table.grid(padding=0, collapse_padding=True)
        row.add_column(width=GUTTER_WIDTH, no_wrap=True)
        cells = [Text(f"{DOT}  ", style=getattr(palette, "gutter" if dim else dot_role, ""))]
        if self.label:
            row.add_column(width=LABEL_WIDTH, no_wrap=True)
            cells.append(
                Text(f"{self.label:<{LABEL_WIDTH}}", style=palette.muted)
            )
        # folded rather than broken on spaces alone: a key or a URL is one word and
        # has to be allowed to continue on the next row
        row.add_column(overflow="fold")
        cells.append(
            Text(
                visual_text(self.body),
                style=getattr(palette, self.role or body_role, palette.fg),
            )
        )
        row.add_row(*cells)
        return row


def status_line(message: str, state: str, palette, *, dim: bool = False) -> Text:
    """The one-line "what is happening right now", dot and all.

    Bold, unlike the log: this is not a record of a run, it is the one thing
    happening in it, and it is read at a glance rather than scanned through.
    """
    dot_role, body_role = STATE_STYLE[normalise(state)]
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(f"{DOT}  ", style=getattr(palette, "gutter" if dim else dot_role, ""))
    line.append(
        visual_text(safe_terminal_text(message)),
        style=f"bold {getattr(palette, body_role, palette.fg)}",
    )
    return line


__all__ = [
    "DOT",
    "GUTTER_WIDTH",
    "LABEL_WIDTH",
    "LIVE_STATE",
    "STATE_STYLE",
    "LogLine",
    "normalise",
    "safe_terminal_text",
    "status_line",
]

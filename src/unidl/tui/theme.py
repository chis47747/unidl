"""Palettes and stylesheet.

The dark palette uses a neutral near-black base with a violet accent, chosen
because it quantizes cleanly on 256-colour and 16-colour terminals rather than
depending on truecolor. The light counterpart is mixed here because a
dark-only interface is unreadable in a light terminal.

Layout conventions come from the same project's `pager.toml` defaults:
`outer_vpad = 1`, `outer_hpad = 2`, an accent bar to the left of a block with
two columns between it and the content, and a scrollbar sitting flush against
the right edge.

Colours are referenced as Textual design tokens - `$accent`, `$muted`, `$gutter`
- in both the stylesheet and in widget markup, so switching theme repaints
everything without a restart. The only place raw hex is still needed is Rich
``Text`` written into the log, which Rich renders without knowing about Textual's
tokens; :func:`palette_for` hands those call sites the live values.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.theme import Theme


@dataclass(frozen=True)
class Palette:
    """Every colour the interface uses, named by what it means.

    Named by role rather than by hue - ``manifest`` rather than ``blue`` - so the
    light palette can pick a different hue for the same job without every call
    site lying about it.
    """

    name: str
    dark: bool

    # text
    fg: str  # primary
    fg2: str  # secondary
    muted: str  # captions, labels
    dim: str  # hints
    gutter: str  # dimmest: numbers, rules, structure

    # meaning
    accent: str  # selection, keys, titles
    manifest: str  # URLs
    ok: str
    warn: str
    error: str

    # exceptional stream-format badges
    dv: str
    hdr10: str
    hdr10plus: str
    hlg: str
    hdr_vivid: str
    atmos: str
    audio_vivid: str

    #: Lines rather than letters: the rule under the log, and the scrollbar thumb.
    #: Its own role because it is the one colour that is *not* text, and it used to
    #: borrow ``highlight`` - a selected-row background, which against the page is
    #: a ratio of 1.2 and a separator nobody can see. Kept quiet on purpose: a
    #: divider that competes with the words is worse than a faint one.
    rule: str

    # surfaces
    bg: str  # base
    panel: str  # recessed blocks
    highlight: str  # selected row
    hover: str
    visual: str
    scroll_bg: str
    scroll_fg: str


#: The dark palette. Its grey ramp used to bottom out at ``#414141``, which against
#: a near-black page is a ratio of 1.8 - so the platform numbers, the filter hint
#: and the section captions were drawn but not legible. The three quiet steps are
#: now light enough to read while still reading as quiet; ``rule`` is the only one
#: allowed to be faint, and it draws lines rather than words.
DARK = Palette(
    name="dark",
    dark=True,
    fg="#e1e1e1",
    fg2="#c8c8c8",
    muted="#9c9c9c",
    dim="#868686",
    # the row numbers live here, and a number you are asked to type has to be as
    # readable as the name beside it - on the page and on the row under the pointer
    gutter="#848484",
    accent="#bb9af7",
    manifest="#7aa2f7",
    ok="#9ece6a",
    warn="#e0af68",
    error="#f7768e",
    dv="#bb9af7",
    hdr10="#e0af68",
    hdr10plus="#ff9e64",
    hlg="#7aa2f7",
    hdr_vivid="#2ac3de",
    atmos="#9ece6a",
    audio_vivid="#d386b7",
    rule="#525252",
    bg="#141414",
    panel="#1c1c1c",
    highlight="#242424",
    hover="#2c2c2c",
    visual="#363636",
    scroll_bg="#111111",
    # a thumb you can find: the old #242424 was a ratio of 1.2 against the trough
    scroll_fg="#616161",
)

#: The light counterpart, and not a copy of the dark one with the greys flipped.
#:
#: Two things were wrong with the first attempt at it, both of which made a white
#: terminal hard to read. The grey ramp was lifted straight from the dark palette's
#: proportions - ``#b4b4b4`` for the dimmest text - and a light grey on white is a
#: ratio of 2, so the platform numbers and the "156 available" caption were there
#: but invisible. And the page itself was ``#fbfbfb``, near enough pure white to
#: glare, which makes saturated ink beside it harder rather than easier to read.
#:
#: So: an off-white page with a little warmth in it, a grey ramp where every step
#: is still legible against that page (4.5:1 or better, which is what WCAG asks of
#: body text), and hues held at roughly the same strength as each other rather than
#: at maximum saturation. ``rule`` and the scrollbar are the only things allowed to
#: be faint, because they are lines rather than words.
LIGHT = Palette(
    name="light",
    dark=False,
    fg="#1b1d21",
    fg2="#34373c",
    muted="#4f535a",
    dim="#5f636a",
    # the row numbers live here, and a number you are asked to type has to be as
    # readable as the name beside it
    gutter="#63676e",
    accent="#5b3aa6",
    manifest="#1f4fa0",
    ok="#2f6b21",
    warn="#7a4f08",
    error="#9c2233",
    dv="#5b3aa6",
    hdr10="#7a4f08",
    hdr10plus="#94430a",
    hlg="#1f4fa0",
    hdr_vivid="#08727f",
    atmos="#2f6b21",
    audio_vivid="#8a2b72",
    rule="#a9a6a0",
    bg="#f7f6f3",
    panel="#edebe6",
    # The tinted surfaces are kept pale, because whatever is drawn on them has to
    # stay readable: a selected row is a row you are about to read, and a deeper
    # tint bought a stronger selection at the cost of the text on it.
    highlight="#e7e2f6",
    hover="#ecebe6",
    visual="#ded8f3",
    scroll_bg="#edebe6",
    scroll_fg="#98948c",
)

PALETTES: dict[str, Palette] = {"dark": DARK, "light": LIGHT}


def palette_for(mode: str) -> Palette:
    """The palette for a setting value, defaulting to dark."""
    return PALETTES.get(str(mode or "").lower(), DARK)


def textual_theme(palette: Palette) -> Theme:
    """A Textual theme carrying our own tokens as CSS variables.

    The standard slots are filled in too, so widgets we do not style ourselves -
    the command palette, tooltips, notifications - still follow the theme.
    """
    return Theme(
        name=palette.name,
        primary=palette.accent,
        secondary=palette.manifest,
        accent=palette.accent,
        success=palette.ok,
        warning=palette.warn,
        error=palette.error,
        foreground=palette.fg,
        background=palette.bg,
        surface=palette.panel,
        panel=palette.highlight,
        dark=palette.dark,
        variables={
            "fg2": palette.fg2,
            "muted": palette.muted,
            "dim": palette.dim,
            "gutter": palette.gutter,
            "manifest": palette.manifest,
            "ok": palette.ok,
            "warn": palette.warn,
            "bad": palette.error,
            "dv": palette.dv,
            "hdr10": palette.hdr10,
            "hdr10plus": palette.hdr10plus,
            "hlg": palette.hlg,
            "hdr-vivid": palette.hdr_vivid,
            "atmos": palette.atmos,
            "audio-vivid": palette.audio_vivid,
            "rule": palette.rule,
            "base": palette.bg,
            "recessed": palette.panel,
            "highlight": palette.highlight,
            "hover": palette.hover,
            "visual": palette.visual,
            "scroll-bg": palette.scroll_bg,
            "scroll-fg": palette.scroll_fg,
        },
    )


THEMES: dict[str, Theme] = {name: textual_theme(p) for name, p in PALETTES.items()}

#: mode -> the registered Textual theme name
THEME_NAMES: dict[str, str] = {name: p.name for name, p in PALETTES.items()}

#: `◆` marks a default bullet; `▸` marks something expandable.
BULLET = "◆"
ARROW = "▸"

# ---------------------------------------------------------------- back-compat
# The verification scripts build Rich Text without an application, so they need
# literal values. Dark is the default, which is what those scripts assume.
FG = DARK.fg
FG_DARK = DARK.fg2
GRAY = DARK.muted
GRAY_DIM = DARK.dim
GRAY_BRIGHT = "#787878"
GUTTER = DARK.gutter
ACCENT = DARK.accent
BLUE = DARK.manifest
GREEN = DARK.ok
YELLOW = DARK.warn
RED = DARK.error
CYAN = "#7dcfff"
ORANGE = "#ff9e64"
TEAL = "#1abc9c"
BG_BASE = DARK.bg
BG_DARK = DARK.panel
BG_HIGHLIGHT = DARK.highlight
BG_HOVER = DARK.hover
BG_VISUAL = DARK.visual


CSS = """
/* ---------------------------------------------------------------- surface */
Screen {
    background: $base;
    color: $foreground;
    layout: vertical;
}

* {
    scrollbar-background: $scroll-bg;
    scrollbar-background-hover: $scroll-bg;
    scrollbar-background-active: $scroll-bg;
    scrollbar-color: $scroll-fg;
    scrollbar-color-hover: $dim;
    scrollbar-color-active: $accent;
    scrollbar-size-vertical: 1;
    scrollbar-size-horizontal: 1;
}

/* ----------------------------------------------------------------- chrome
   Terminals do not let an application change the font size, so the chrome is
   made to read as a row of controls through weight, a panel background and
   generous horizontal padding instead. */
#chrome {
    height: 1;
    padding: 0 1;
    background: $recessed;
    color: $fg2;
}
#chrome-gap {
    width: 1fr;
    background: $recessed;
}
.chrome-button {
    width: auto;
    padding: 0 2;
    margin: 0 1 0 0;
    background: $highlight;
    color: $foreground;
    text-style: bold;
}
.chrome-button:hover {
    background: $accent;
    color: $base;
    text-style: bold;
}
.chrome-disabled {
    background: $recessed;
    color: $gutter;
    text-style: none;
}
.chrome-disabled:hover {
    background: $recessed;
    color: $gutter;
    text-style: none;
}

/* ----------------------------------------------------------------- header */
#banner {
    height: auto;
    padding: 1 2 0 2;
    text-align: center;
    color: $accent;
}
#caption {
    height: 2;
    padding: 0 2 1 2;
    align: center middle;
    color: $muted;
}
#caption .caption-link {
    width: auto;
    height: 1;
    padding: 0 1;
}
#caption .caption-link:hover {
    background: $highlight;
    color: $foreground;
    text-style: underline;
}
#caption .caption-separator {
    width: auto;
    height: 1;
    color: $gutter;
}
/* A row of air above it, because it belongs to the field below rather than to
   the list above: without one, the sentence explaining the box read as the last
   line of the platform grid. */
#prompt-hint {
    height: 2;
    padding: 1 1 0 1;
    color: $gutter;
}

#masthead {
    height: 2;
    padding: 1 3 0 3;
    color: $foreground;
    text-style: bold;
}
#subhead {
    height: 1;
    padding: 0 3;
    color: $muted;
}
#crumbs {
    height: 1;
    padding: 0 3;
    color: $muted;
}

/* ------------------------------------------------ service / flow / delivery
   The title shrinks as you go deeper: six rows of block letters on the main
   screen, three here, plain bold text on screens 3 and 4. */
#service-banner {
    height: auto;
    padding: 1 2 0 2;
    text-align: center;
    color: $accent;
}
#ident-line {
    height: 2;
    padding: 1 2 0 2;
    color: $foreground;
}
#param-line {
    height: 1;
    padding: 0 2;
    color: $muted;
}
#status-line {
    height: 1;
    padding: 0 2;
    color: $dim;
}
#delivery-head-row {
    height: 2;
    padding: 1 2 0 2;
}
#delivery-head {
    width: 1fr;
    height: 1;
    padding: 0;
    color: $foreground;
}
#delivery-elapsed {
    height: 1;
    padding: 0 2;
    color: $muted;
}

/* A compact, scrollable delivery contract: the resolved output path, selected
   streams, their encrypted KIDs and the KID:KEY pairs handed to the native
   downloader. It stays readable without forcing the live progress or log off a
   short terminal. */
#delivery-details {
    display: none;
    width: 1fr;
    height: 1fr;
    min-height: 3;
    margin: 0 2;
    padding: 0 1;
    border-left: solid $rule;
    background: $recessed;
    scrollbar-size: 1 1;
}

/* Chapters open as a popup over the track picker or delivery screen. The job
   underneath is not paused or cancelled; ✕ / esc returns to that same screen. */
ChaptersScreen {
    align: center middle;
    background: $base 70%;
}
#chapter-modal {
    width: 88;
    max-width: 94%;
    height: auto;
    max-height: 80%;
    background: $recessed;
    border-left: solid $accent;
    padding: 1 1 1 1;
}
#chapter-modal-head {
    height: 1;
    padding: 0 1;
}
#chapter-modal-title {
    width: 1fr;
    height: 1;
    color: $foreground;
    text-style: bold;
}
#chapter-modal-close {
    width: 5;
    height: 1;
    content-align: right middle;
    color: $accent;
    text-style: bold;
}
#chapter-modal-close:hover {
    background: $highlight;
    color: $foreground;
}
#chapter-summary {
    height: auto;
    padding: 1 2 1 2;
    color: $muted;
}
#chapter-list {
    width: 1fr;
    height: 1fr;
    max-height: 24;
    padding: 0 1 1 1;
    overflow-x: hidden;
    scrollbar-size-horizontal: 0;
}
#chapter-modal-hint {
    height: 1;
    padding: 0 2 0 2;
    color: $muted;
}
.chapter-row {
    width: 1fr;
    height: auto;
    padding: 1 1;
    color: $foreground;
}
.chapter-row.odd {
    background: $panel;
}
LyricsScreen {
    align: center middle;
    background: $base 70%;
}
#lyrics-modal {
    width: 88;
    max-width: 94%;
    height: auto;
    max-height: 80%;
    background: $recessed;
    border-left: solid $accent;
    padding: 1;
}
#lyrics-modal-head {
    height: 1;
    padding: 0 1;
}
#lyrics-modal-title {
    width: 1fr;
    height: 1;
    color: $foreground;
    text-style: bold;
}
#lyrics-modal-close {
    width: 5;
    height: 1;
    content-align: right middle;
    color: $accent;
    text-style: bold;
}
#lyrics-modal-close:hover {
    background: $highlight;
    color: $foreground;
}
#lyrics-summary {
    height: auto;
    padding: 1 2;
    color: $muted;
}
#lyrics-list {
    width: 1fr;
    height: 1fr;
    max-height: 24;
    padding: 0 1 1 1;
    overflow-x: hidden;
    scrollbar-size-horizontal: 0;
}
#lyrics-modal-hint {
    height: 1;
    padding: 0 2;
    color: $muted;
}
.lyrics-row {
    width: 1fr;
    height: auto;
    padding: 1;
    color: $foreground;
}
.lyrics-row.odd {
    background: $panel;
}
.ask-title-row {
    height: 1;
    width: 1fr;
    padding: 0;
}
.ask-title-row .ask-title {
    width: auto;
    max-width: 1fr;
}
.chapter-chip {
    width: auto;
    height: 1;
    padding: 0 1;
    color: $accent;
    text-style: bold;
}
.chapter-chip:hover {
    background: $highlight;
    color: $foreground;
}
#delivery-details-body {
    width: 1fr;
    height: auto;
    color: $fg2;
}

/* Audio-only delivery keeps artwork and ID3 facts on the left, with live
   progress on the right. The rail stays hidden for ordinary VOD and live. */
#delivery-main {
    width: 1fr;
    height: 1fr;
    min-height: 0;
}
#delivery-main-left {
    width: 1fr;
    height: 1fr;
    min-height: 0;
    min-width: 0;
}
#delivery-audio-side {
    display: none;
    width: 44;
    min-width: 40;
    height: 1fr;
    max-height: 100%;
    margin: 1 1 0 0;
    padding: 1 1;
    border: round $accent;
    background: $recessed;
}
#delivery-audio-side .audio-cover {
    height: 1fr;
}

/* Shared audio preview used by the track picker and the download side rail. */
.audio-track-picker {
    width: 1fr;
    height: 1fr;
    min-height: 0;
}
.audio-track-preview {
    width: 42;
    min-width: 30;
    height: 1fr;
    min-height: 0;
    max-height: 100%;
    padding: 0 1;
    border-right: solid $rule;
    overflow-y: auto;
}
.audio-track-controls {
    width: 1fr;
    height: 1fr;
    min-width: 0;
    padding: 0 1;
}
.audio-cover-label {
    width: 100%;
    height: 1;
    color: $muted;
    text-style: bold;
}
.audio-cover {
    width: auto;
    min-width: 8;
    max-width: 100%;
    height: auto;
    min-height: 4;
    max-height: 18;
    color: $foreground;
    /*
       The artwork is rendered with background-coloured spaces.  Keeping the
       surface transparent still matters for the loading and unavailable
       states, and lets the parent card provide one consistent background.
    */
    background: transparent;
    text-align: center;
    content-align: center middle;
}
.audio-metadata {
    width: 100%;
    height: 1fr;
    min-height: 1;
    max-height: 1fr;
    padding: 1 0 0 0;
    color: $fg2;
    overflow-y: auto;
}

/* UniDL's own display, repainted in place. A card like the manual-activation
   panel, for the same reason: this is the thing that is happening, and it used to
   arrive as a wall of near-identical log lines. Hidden until there is a frame, so
   the screen does not hold an empty box open. */
#delivery-frame {
    display: none;
    width: 1fr;
    height: auto;
    /* Border and vertical padding consume four rows before progress text gets a
       cell of its own.  A three-row minimum therefore produced the exact empty
       outlined card seen at 135x34.  Keep one body row even on short terminals,
       and let an ordinary three-track download use three body rows. */
    min-height: 5;
    max-height: 60%;
    margin: 1 2 0 2;
    padding: 1 2;
    border: round $accent;
    background: $recessed;
    color: $foreground;
}
/* the batch queue: hidden for a single title, where it would only restate the
   header above it */
#queue-summary {
    display: none;
    height: 2;
    padding: 1 2 0 2;
}
#queue-list {
    display: none;
    height: auto;
    max-height: 40%;
    padding: 0 1;
}
.queue-row {
    height: 1;
    padding: 0 1;
}
.queue-row:hover {
    background: $hover;
}

DownloadScreen #ask-area {
    height: auto;
    max-height: 20%;
}

#statusline {
    height: 1;
    padding: 0 2;
}
.pill {
    width: auto;
    padding: 0 1;
    color: $muted;
}
.pill:hover {
    background: $hover;
    color: $foreground;
}

/* ------------------------------------------------------------------- body */
#body {
    height: 1fr;
    padding: 1 2 0 2;
}

/* Three rows, not one. A field one row tall next to a full-width background is
   a coloured line, and a coloured line does not read as somewhere to type - it
   reads as a rule. The row above and below the text are the box, which is why
   they are padding rather than margin: they carry its background. */
#filter, #query {
    height: 3;
    border: none;
    padding: 1 2;
    background: $recessed;
    color: $foreground;
}
#filter:focus, #query:focus {
    background: $highlight;
}
Input {
    border: none;
    padding: 0 1;
    background: $recessed;
    color: $foreground;
    /* Textual's Input scrolls its contents horizontally itself.  A visible
       scrollbar consumes a content row; most of our fields are exactly one
       content row high, so the global one-row scrollbar used by lists replaced
       a long value with its trough and thumb. */
    scrollbar-size-horizontal: 0;
}
Input:focus {
    background: $highlight;
}
/* Bold, and $muted rather than $dim: this is the only thing in an empty field,
   so it is a sentence to read rather than a hint to notice - and the two rules
   in this file that are dimmer than it are for separators. Weight does the work
   colour cannot: it stays legible in a bright terminal, where a grey placeholder
   is a smudge. */
Input > .input--placeholder {
    color: $muted;
    text-style: bold;
}
Input > .input--cursor {
    background: $accent;
    color: $base;
}

/* ---------------------------------------------------------- manual key entry */
#vault-add-body {
    height: 1fr;
    padding: 1 2;
    align-horizontal: center;
}
#vault-add-card {
    width: 90;
    max-width: 96%;
    height: auto;
    padding: 1 2;
    border: round $accent;
    background: $recessed;
}
.vault-field-label {
    height: 2;
    padding: 1 0 0 0;
    color: $muted;
}
#vault-service-query, #vault-title {
    height: 3;
}
#vault-service-state {
    width: 100%;
    height: 1;
    padding: 0 1;
    color: $dim;
}
#vault-service-list {
    width: 100%;
    height: 6;
    border: none;
    background: $highlight;
}
#vault-service-list > .option-list--option-highlighted {
    background: $visual;
    color: $foreground;
}
#vault-pairs {
    height: 9;
    min-height: 5;
    border: none;
    border-left: solid $accent;
    padding: 0 1;
    background: $highlight;
    color: $foreground;
}
#vault-pairs:focus {
    background: $visual;
}
#vault-validation {
    width: 100%;
    height: auto;
    max-height: 7;
    padding: 1 0 0 0;
    color: $dim;
}
#vault-validation.warning {
    color: $warn;
}
#vault-validation.error {
    color: $bad;
}
#vault-destination-row {
    width: 100%;
    height: auto;
    margin: 1 0 0 0;
    align: left middle;
}
#vault-destination-state {
    width: 1fr;
    height: auto;
    padding: 0 1;
    color: $foreground;
}
#vault-destination-choose {
    width: auto;
    min-width: 16;
}
#vault-add-actions {
    width: 100%;
    height: 3;
    align: right middle;
}
#vault-add-actions Button {
    width: auto;
    min-width: 14;
    margin: 0 0 0 1;
}

/* -------------------------------------------------------- responsive grid */
#grid-area {
    height: 1fr;
    padding: 0;
}
/* Air on both sides of it: a heading touching its own first row reads as part of
   the list, and the whole point of it is to be the thing above the list. */
.section-head {
    height: 3;
    padding: 1 1 1 1;
    color: $dim;
}
#service-list-head {
    width: 100%;
}
#service-list-summary {
    width: 1fr;
    height: 1;
}
#service-view {
    width: auto;
    height: 1;
    padding: 0 1;
}
#service-view:hover {
    background: $highlight;
    color: $foreground;
    text-style: underline;
}
.empty-note {
    width: 1fr;
    height: auto;
    padding: 1 1 0 1;
    color: $dim;
}
/* What is not set up yet, on the screen you land on. Hidden the moment it is
   dealt with, so a working install never sees it. A left bar rather than a full
   border: it is a note beside the list, not a dialog in front of it. */
#setup-note, #service-setup-note {
    display: none;
    width: 1fr;
    height: auto;
    text-wrap: wrap;
    padding: 0 1;
    margin: 0 0 1 0;
    border-left: solid $warn;
    background: $recessed;
}
/* it is a chip, so it inherits the chip hover: the background is the affordance
   here, and bolding two lines of prose is not */
#setup-note:hover, #service-setup-note:hover {
    background: $highlight;
    text-style: none;
}
/* One blank row between rows of platforms. A hundred and fifty-six single-line
   cells stacked with nothing between them is a wall of text: the eye cannot find
   the row it is on, and the number that addresses a cell belongs to whichever row
   you happen to be reading. It halves how many are on screen at once, which is
   the right trade - they are reached by typing a name or a number far more often
   than by being scrolled past. */
.service-grid {
    height: auto;
    grid-size: 1;
    grid-rows: 1;
    grid-gutter: 1 1;
}
.service-cell {
    height: 1;
    padding: 0 1;
    color: $fg2;
}
.service-cell:hover {
    background: $hover;
    color: $foreground;
}
.service-cell:focus {
    background: $highlight;
    color: $foreground;
    text-style: bold;
}
/* the cell a typed number points at, before Enter confirms it */
.service-cell.targeted {
    background: $highlight;
    color: $accent;
}

/* -------------------------------------------------------------- selection */
OptionList, SelectionList, DataTable {
    height: 1fr;
    border: none;
    padding: 0;
    background: $base;
    color: $fg2;
}
OptionList:focus, SelectionList:focus, DataTable:focus {
    border: none;
}
OptionList > .option-list--option {
    padding: 0 1;
}
OptionList > .option-list--option-highlighted,
SelectionList > .option-list--option-highlighted {
    background: $highlight;
    color: $foreground;
    text-style: none;
}
OptionList:focus > .option-list--option-highlighted,
SelectionList:focus > .option-list--option-highlighted {
    background: $highlight;
    color: $foreground;
    text-style: bold;
}
OptionList > .option-list--option-hover,
SelectionList > .option-list--option-hover {
    background: $hover;
    color: $foreground;
}
OptionList > .option-list--option-disabled {
    color: $gutter;
}
/* An unticked box is the same glyph as a ticked one, drawn in the background
   colour. Colour is the whole distinction, which is why the palette keeps a
   real contrast between $accent and $recessed in both modes. */
SelectionList > .selection-list--button {
    background: $recessed;
    color: $recessed;
}
SelectionList > .selection-list--button-selected {
    background: $recessed;
    color: $accent;
}
SelectionList > .selection-list--button-highlighted {
    background: $highlight;
    color: $highlight;
}
SelectionList > .selection-list--button-selected-highlighted {
    background: $highlight;
    color: $accent;
}
DataTable > .datatable--header {
    background: $base;
    color: $gutter;
    text-style: none;
}
DataTable > .datatable--cursor {
    background: $highlight;
    color: $foreground;
}
DataTable > .datatable--hover {
    background: $hover;
}

/* -------------------------------------------------------------------- ask */
#ask-area {
    height: 1fr;
    padding: 0;
}
/* a single field belongs at the bottom, next to where you are typing */
#ask-area.bottom {
    align: left bottom;
}
TextWidget {
    height: auto;
}
.ask-title {
    height: 1;
    color: $foreground;
    text-style: bold;
    padding: 0 1 0 1;
}
/* height 2 with a row of bottom padding: at height 1 the padding ate the only
   content row and every hint rendered blank */
.ask-hint {
    height: 2;
    color: $dim;
    padding: 0 1 1 1;
}
/* an unbordered empty input looks like empty space; the accent bar says
   "the cursor is here", and the three rows are what make it a box rather than a
   coloured line. Same height as the filter on the main screen, because they are
   the same act: this is where the answer goes. */
.ask-input {
    height: 3;
    border: none;
    border-left: solid $accent;
    padding: 1 2;
    background: $highlight;
    color: $foreground;
}
/* Except in a form, where each field already has its own label above it and the
   card holds two of them: three rows each would make a sign-in card mostly box. */
.form-input {
    height: 1;
    padding: 0 1;
}
.ask-input:focus {
    background: $visual;
}
/* Something to go and do by hand - a code to type, a link to open, a name to
   pick off a list on another device. It goes in the middle of the screen in a
   bordered card, because the log is where you look for what already happened,
   not for what the app is waiting on you to do. */
#ask-area.centre {
    align: center middle;
}
/* A card too tall to centre: still in the middle of the screen left to right,
   but starting at the top, because a centred card taller than its container
   loses its first rows off the top where nothing can scroll back to them. */
#ask-area.centre-top {
    align: center top;
}
/* A definite width rather than `auto`: the widest row in here is a URL or a
   sentence a service wrote, and an auto-sized card clipped whichever one of them
   was longest instead of wrapping it. */
.manual-card {
    width: 78;
    max-width: 96%;
    height: auto;
    padding: 1 3;
    border: round $accent;
    background: $recessed;
}
/* the field belongs inside the card when the card is what it is answering, and
   as wide as the values above it rather than as wide as its own placeholder */
.manual-card .ask-input {
    width: 100%;
    margin: 1 0 0 1;
}
/* a field that has its own label above it: the label brings the row of air, so a
   second one here would double the gap between the pairs of a form */
.manual-card .form-input {
    margin: 0 0 0 1;
}
/* Full width, not `auto`, for every row of prose in a card: the card is a fixed
   78 columns and anything wider than it is clipped, so a hint or a title that ran
   long lost its ending mid-word. Given a width they wrap instead. */
.await-title {
    width: 100%;
    height: auto;
    color: $foreground;
    text-style: bold;
}
/* the number is what a keypress copies, so it is part of the label */
.await-label {
    width: 100%;
    height: auto;
    padding: 1 0 0 0;
}
/* bold and on its own row, with a background nothing else in the card has: this
   is the one thing on screen that has to be read exactly. Bold rather than
   larger, because a terminal has one font at one size. */
.await-value {
    width: auto;
    height: auto;
    padding: 0 1;
    margin: 0 0 0 1;
    color: $accent;
    background: $visual;
    text-style: bold;
}
/* a URL is not typed, it is followed, so it gets the colour URLs have
   everywhere else here rather than the "type this" block */
.await-link {
    color: $manifest;
    background: $recessed;
}
/* A value too long for one row of the card. `width: auto` sizes to the content,
   and content wider than a fixed-width card is clipped - so a full UniDL
   command line lost everything past column 70. Given a width it wraps instead. */
.await-value.await-wrap {
    width: 100%;
}
.await-keys {
    width: 100%;
    height: auto;
    padding: 1 0 0 0;
}
/* a continuation of the line above it, so no row of air between them */
.await-keys.thin {
    padding: 0;
}
/* prose in the card, and what a service that names nothing still gets */
.await-line {
    width: 100%;
    height: auto;
    padding: 1 0 0 0;
    color: $accent;
    text-style: bold;
}

/* QR is an image, not a theme surface.  Its paper stays white and its ink
   black under both palettes so a phone camera sees the same contrast. */
.qr-frame {
    width: 100%;
    height: auto;
    align: center middle;
    margin: 1 0 0 0;
}
.qr-protocol-image {
    /* Keep this width in sync with PROTOCOL_QR_WIDTH. Generated QR images use
       an exact cell-sized canvas so non-square terminal cells cannot stretch
       their modules. */
    width: 40;
    max-width: 100%;
    height: auto;
    background: #ffffff;
}
.qr-unicode-image {
    width: auto;
    height: auto;
    color: #000000;
    background: #ffffff;
    text-wrap: nowrap;
}
.qr-render-error {
    width: 100%;
    height: auto;
    color: $warn;
    text-align: center;
}

/* sits under the list it narrows, with a row of air between them */
.filter-box {
    margin: 1 0 0 0;
}

/* ------------------------------------------------------------------- chips
   Status-line items that are also controls. The hover state is the affordance:
   without it there is nothing to say the text can be clicked. */
.chip {
    width: auto;
    padding: 0 2;
    color: $muted;
}
.chip:hover {
    background: $highlight;
    color: $foreground;
    text-style: bold;
}
/* not clickable, so it must not pretend to be */
.chip-static:hover {
    background: transparent;
    color: $muted;
    text-style: none;
}
.chip-sep {
    width: auto;
    color: $gutter;
}

/* -------------------------------------------------------------------- log */
#log-pane {
    height: 10;
    padding: 1 1 0 1;
    border-top: solid $rule;
}
#log-pane.tall {
    height: 2fr;
}
/* header only: the log is still there and still says how much is in it */
#log-pane.collapsed {
    height: 3;
}
#log-pane.collapsed RichLog {
    display: none;
}
/* nothing logged yet: take no space rather than showing an empty box */
#log-pane.empty {
    display: none;
}
/* The delivery screen keeps a compact log while work is active, leaving room
   for the selected streams and structured progress. The header can still make
   it full-height explicitly. */
#log-pane.delivery {
    /* Size the normal pane to the lines it currently contains. The old fixed
       ten-row pane left a conspicuous empty rectangle under a short log while a
       download was running. The cap keeps a busy session from crowding out the
       structured progress card; ^l still expands it to the available height. */
    height: auto;
    max-height: 10;
}
#log-pane.delivery RichLog {
    height: auto;
    max-height: 8;
}
/* The delivery selector has the same specificity as the state selectors above;
   keep these explicit overrides after it so a live job can resize the pane too. */
#log-pane.delivery.tall {
    height: 1fr;
    max-height: 100h;
}
#log-pane.delivery.tall RichLog {
    height: 1fr;
    max-height: 100h;
}
#log-pane.delivery.collapsed {
    height: 3;
}
#log-pane.delivery.collapsed RichLog {
    display: none;
}
#log-header {
    height: 1;
}

/* what the content area says when there is nothing to answer */
#waiting {
    width: 1fr;
    height: auto;
    padding: 1 1;
    color: $dim;
}

/* A failure belongs where you are looking, not only in a log you may have
   collapsed. Hidden until there is one, so it costs no rows. */
#error-panel {
    display: none;
    width: 1fr;
    height: auto;
    margin: 0 0 1 0;
    padding: 1 2;
    background: $recessed;
    border-left: solid $bad;
}

RichLog {
    background: $base;
    color: $muted;
}

/* ----------------------------------------------------------------- keybar
   Two rows, the text on the second. The bar is a dense line of chords and it sat
   flush against whatever was above it - a field, a list, a log - which made it
   read as another row of that thing rather than as the frame. The blank row is
   the whole difference between a footer and a squashed last line. */
#keybar {
    height: 2;
    padding: 1 2 0 2;
    background: $base;
    color: $dim;
}

/* ---------------------------------------------------------------- overlay */
Toast {
    background: $highlight;
    color: $foreground;
    border: none;
    border-left: solid $accent;
    padding: 0 1;
}
Toast.-error {
    border-left: solid $bad;
}
Toast.-warning {
    border-left: solid $warn;
}
ToastRack {
    align: right bottom;
}

CommandPalette > Vertical {
    background: $recessed;
    border: none;
}
CommandPalette #--input {
    background: $highlight;
}
CommandPalette #--container {
    background: $recessed;
}

ModalScreen {
    background: $base 70%;
    align: center middle;
}
/* See-through overlay cards hide the parent chrome. The ✕ in the card
   header is the live dismiss control; the Back/Quit labels behind it are not. */
.modal-head {
    width: 100%;
    height: 1;
    margin: 0 0 1 0;
}
.modal-head > Label, .modal-head > Static {
    width: 1fr;
    height: 1;
    padding: 0;
}
.modal-close {
    width: 5;
    height: 1;
    content-align: right middle;
    color: $accent;
    text-style: bold;
}
.modal-close:hover {
    background: $highlight;
    color: $foreground;
}
/* carries its own chrome, so it must not show the screen below's as well */
ModalScreen.editor {
    background: $base;
    align: left top;
}
#about-modal, #update-modal {
    width: 86;
    max-width: 94%;
    height: auto;
    max-height: 82%;
    padding: 1 2;
    background: $recessed;
    border-left: solid $accent;
}
#about-modal-head, #update-modal-head {
    width: 100%;
    height: 2;
}
#about-modal-title, #update-modal-title {
    width: 1fr;
    height: 1;
    color: $foreground;
    text-style: bold;
}
#about-modal-close, #update-modal-close {
    width: 3;
    height: 1;
    text-align: center;
    color: $muted;
}
#about-modal-close:hover, #update-modal-close:hover {
    background: $highlight;
    color: $foreground;
}
#about-intro, #about-license-note, #update-status, #update-result {
    width: 100%;
    height: auto;
    padding: 0 0 1 0;
    color: $muted;
}
#about-list {
    width: 100%;
    height: 1fr;
    min-height: 8;
    max-height: 28;
    padding: 0 1;
    background: $base;
}
.about-row {
    width: 100%;
    height: auto;
    min-height: 3;
    padding: 1 0 0 0;
}
#update-current {
    width: 100%;
    height: 2;
}
#update-links {
    width: 100%;
    height: 2;
    padding: 0 0 1 0;
    align: left middle;
}
#update-links .chip {
    width: auto;
    margin-right: 1;
}
#update-result {
    border-left: solid $ok;
    padding: 0 1 1 1;
    color: $foreground;
}
#update-notes-title {
    width: 100%;
    height: 2;
    padding: 1 0 0 0;
    color: $foreground;
    text-style: bold;
}
#update-notes {
    width: 100%;
    height: auto;
    max-height: 12;
    padding: 0 1;
    background: $base;
}
.update-note {
    width: 100%;
    height: auto;
    padding: 0 0 1 0;
}
#about-modal-hint, #update-modal-hint {
    width: 100%;
    height: 2;
    padding: 1 0 0 0;
    color: $dim;
}
#modal-body {
    height: 1fr;
    align: center middle;
}
#modal-card {
    width: 72;
    max-width: 90%;
    height: auto;
    max-height: 70%;
    margin: 1 2;
    padding: 1 2;
    background: $recessed;
    border-left: solid $accent;
}
#modal-card OptionList {
    height: auto;
    max-height: 20;
    background: $recessed;
}
/* Help text is a sentence. Label defaults to width:auto, which sizes to the
   text and then gets clipped by the card instead of wrapping inside it. */
#modal-card .ask-title, #modal-card .ask-hint {
    width: 1fr;
    height: auto;
    padding: 0 1 1 1;
}
.multi-setting-title-row {
    width: 100%;
    height: auto;
    min-height: 2;
}
#modal-card .multi-setting-title-row .ask-title {
    width: 1fr;
    max-width: 1fr;
}
#multi-setting-confirm {
    width: auto;
    height: 1;
    padding: 0 2;
    background: $accent;
    color: $base;
    text-style: bold;
}
#multi-setting-confirm:hover {
    background: $visual;
    color: $foreground;
}

/* ------------------------------------------------ multi-vault destination */
#vault-target-card {
    width: 82;
    max-width: 92%;
    height: auto;
    max-height: 82%;
    padding: 1 2 0 2;
    background: $recessed;
    border: round $rule;
}
#vault-target-title {
    width: 1fr;
    height: 1;
    padding: 0;
    color: $accent;
    text-style: bold;
}
#vault-target-help {
    width: 100%;
    height: auto;
    max-height: 3;
    padding: 0 0 1 0;
    color: $muted;
}
#vault-target-summary {
    width: 100%;
    height: 1;
    margin: 0 0 1 0;
    padding: 0 1;
    align: left middle;
    background: $base;
}
#vault-target-all {
    width: 1fr;
    height: 1;
    min-height: 1;
    border: none;
    border-left: solid transparent;
    padding: 0 1;
    margin: 0;
    background: transparent;
}
#vault-target-all:focus {
    border-left: solid $accent;
    background: $highlight;
}
#vault-target-all > .toggle--button {
    width: 4;
    min-width: 4;
    color: $muted;
    background: transparent;
}
#vault-target-all.-on > .toggle--button {
    color: $ok;
    background: transparent;
    text-style: bold;
}
#vault-target-all > .toggle--label {
    color: $foreground;
    text-style: bold;
}
#vault-target-all:focus > .toggle--label {
    color: $foreground;
    background: transparent;
    text-style: bold;
}
#vault-target-count {
    width: auto;
    height: 1;
    padding: 0 1;
    color: $muted;
}
#vault-target-list {
    width: 100%;
    height: auto;
    max-height: 16;
    background: $base;
    border: round $rule;
    padding: 0 1;
    scrollbar-size: 1 1;
}
.vault-target-group {
    width: 100%;
    height: 1;
    padding: 0 1;
    margin: 1 0 0 0;
    background: $recessed;
    text-style: bold;
}
.vault-target-option {
    width: 100%;
    height: 1;
    min-height: 1;
    border: none;
    border-left: solid transparent;
    padding: 0 1;
    margin: 0;
    background: $base;
    color: $fg2;
}
.vault-target-option > .toggle--button {
    width: 4;
    min-width: 4;
    color: $muted;
    background: transparent;
}
.vault-target-option.-on > .toggle--button {
    color: $ok;
    background: transparent;
    text-style: bold;
}
.vault-target-option > .toggle--label {
    color: $fg2;
}
.vault-target-option.-on > .toggle--label {
    color: $foreground;
}
.vault-target-option:focus {
    border-left: solid $accent;
    background: $highlight;
    color: $foreground;
}
.vault-target-option:focus > .toggle--label {
    color: $foreground;
    background: transparent;
    text-style: bold;
}
#vault-target-empty {
    width: 100%;
    height: 3;
    padding: 1 1;
    color: $muted;
}
#vault-target-actions {
    width: 100%;
    height: 3;
    align: right middle;
    margin: 1 0 0 0;
}
#vault-target-actions Button {
    width: auto;
    height: 1;
    min-height: 1;
    min-width: 11;
    margin: 0 0 0 1;
    padding: 0 2;
    border: none;
}
#vault-target-apply {
    background: $accent;
    color: $base;
    text-style: bold;
    border: none;
}
#vault-target-cancel {
    background: transparent;
    color: $muted;
}
#vault-target-cancel:focus {
    background: $visual;
    color: $foreground;
}

/* The single-choice remote-search dialog reuses the same card, but its list is
   an OptionList rather than checkboxes. Keep its rows visually identical. */
#vault-target-list > .option-list--option {
    padding: 0 1;
    background: $base;
}
#vault-target-list:focus > .option-list--option-highlighted {
    background: $highlight;
    color: $foreground;
}
.short #vault-target-card {
    max-height: 92%;
    padding-left: 1;
    padding-right: 1;
}
.short #vault-target-list {
    max-height: 10;
}
.short #vault-target-actions {
    height: 2;
}

/* ------------------------------------------------------- CDM / vault manager */
#resource-manager-help, #storage-manager-help, #proxy-manager-help, #justwatch-settings-help, #settings-group-help {
    height: auto;
    min-height: 1;
    max-height: 6;
    overflow-y: auto;
    text-wrap: wrap;
    padding: 0 2 1 2;
    color: $muted;
}
#resource-tabs, #storage-tabs, #proxy-tabs {
    height: 2;
    margin: 0 2;
    background: $base;
}
#resource-tabs Tab, #storage-tabs Tab, #proxy-tabs Tab {
    color: $muted;
}
#resource-tabs Tab.-active, #storage-tabs Tab.-active, #proxy-tabs Tab.-active {
    background: $highlight;
    color: $accent;
    text-style: bold;
}
#resource-manager-body {
    height: 1fr;
    padding: 1 2 0 2;
}
#resource-list {
    height: 1fr;
    border: none;
    background: $base;
}
#storage-manager-body {
    height: 1fr;
    padding: 1 2 0 2;
}
#storage-list {
    height: 1fr;
    border: none;
    background: $base;
}
#storage-preview {
    width: 100%;
    height: auto;
    max-height: 3;
    margin: 0 0 1 0;
    padding: 0 1;
    background: $recessed;
    color: $fg2;
}
#storage-editor-preview {
    width: 100%;
    height: auto;
    max-height: 3;
    margin: 1 0 0 0;
    padding: 0 1;
    color: $fg2;
}
#proxy-manager-body, #justwatch-settings-body {
    height: 1fr;
    padding: 1 2 0 2;
}
#proxy-list, #justwatch-settings-list {
    height: 1fr;
    border: none;
    background: $base;
}
#settings-group-list {
    height: 1fr;
    border: none;
    background: $base;
}
#settings-group-list:focus > .option-list--option-highlighted {
    background: $highlight;
    color: $foreground;
}
#proxy-preview {
    width: 100%;
    height: auto;
    max-height: 2;
    margin: 0 0 1 0;
    padding: 0 1;
    background: $recessed;
    color: $fg2;
}
.proxy-editor-card {
    width: 72;
    max-height: 90%;
}
.proxy-field-label {
    margin-top: 1;
    color: $muted;
}
#proxy-provider-types {
    height: auto;
    max-height: 8;
    border: none;
    background: $base;
}
#express-login-status {
    height: auto;
    margin-bottom: 1;
    color: $muted;
}
#express-login-code, #express-login-url {
    height: auto;
    min-height: 2;
    margin-bottom: 1;
    padding: 1 2;
    background: $recessed;
    color: $foreground;
}
#proxy-provider-types:focus > .option-list--option-highlighted,
#proxy-list:focus > .option-list--option-highlighted,
#justwatch-settings-list:focus > .option-list--option-highlighted {
    background: $highlight;
    color: $foreground;
}
#resource-manager-actions, #proxy-manager-actions {
    width: 100%;
    height: 3;
    margin: 1 0 0 0;
    background: $base;
    scrollbar-size-horizontal: 0;
}
#resource-manager-actions Button, #proxy-manager-actions Button {
    width: auto;
    min-width: 10;
    height: 1;
    min-height: 1;
    margin: 0 1 0 0;
    padding: 0 1;
    border: none;
    background: $recessed;
    color: $fg2;
}
#resource-manager-actions Button:focus, #proxy-manager-actions Button:focus {
    background: $visual;
    color: $foreground;
}
#resource-manager-actions Button:disabled, #proxy-manager-actions Button:disabled {
    color: $gutter;
    background: transparent;
}

/* Resource forms intentionally resemble a compact settings card rather than a
   browser form. The cards are modal because a token/password must not remain in
   view behind another screen while the user chooses a resource. */
#resource-editor-card {
    width: 86;
    max-width: 94%;
    height: 90%;
    max-height: 90%;
    padding: 1 2;
    background: $recessed;
    border: round $rule;
}
#resource-policy-card, #resource-confirm-card {
    width: 86;
    max-width: 94%;
    height: auto;
    max-height: 90%;
    padding: 1 2;
    background: $recessed;
    border: round $rule;
}
#resource-editor-title, #resource-policy-title, #resource-confirm-title {
    width: 1fr;
    height: 1;
    padding: 0;
    color: $accent;
    text-style: bold;
}
#resource-editor-help, #resource-policy-help, #resource-confirm-help {
    width: 100%;
    height: auto;
    max-height: 3;
    padding: 0 0 1 0;
    color: $muted;
}
#resource-editor-fields {
    width: 100%;
    height: 1fr;
    padding: 0 1;
    background: $base;
    border: round $rule;
    scrollbar-size: 1 1;
}
.resource-field-label {
    width: 100%;
    height: 1;
    margin: 1 0 0 0;
    color: $muted;
}
#resource-editor-fields Input {
    width: 100%;
    height: 1;
    border: none;
    background: $highlight;
}
#resource-editor-fields Select {
    width: 100%;
    height: 1;
    min-height: 1;
    padding: 0;
    border: none;
    background: transparent;
    color: $foreground;
}
/* Textual's normal SelectCurrent has a three-line `tall` border. The resource
   forms deliberately use compact one-line controls, otherwise its centre line
   paints over the following field label while the Select itself only occupies
   one layout row. */
#resource-editor-fields Select > SelectCurrent {
    width: 100%;
    height: 1;
    padding: 0 1 !important;
    border: none !important;
    border-left: solid $rule !important;
    background: $highlight;
    color: $foreground;
}
#resource-editor-fields Select > SelectCurrent Static#label,
#resource-editor-fields Select > SelectCurrent.-has-value Static#label {
    color: $foreground;
    text-style: bold;
}
#resource-editor-fields Select > SelectCurrent .arrow {
    color: $accent;
}
#resource-editor-fields Select:focus {
    background: transparent;
}
#resource-editor-fields Select:focus > SelectCurrent {
    border-left: solid $accent !important;
    background: $visual;
}
/* The global OptionList rule makes lists fill their parent. A Select menu is an
   overlay, so it must be sized from its options instead; otherwise it becomes a
   one-line strip over the next input. */
#resource-editor-fields Select > SelectOverlay {
    height: auto;
    min-height: 2;
    max-height: 8;
    padding: 0;
    border: round $accent;
    background: $recessed;
    color: $foreground;
}
#resource-editor-fields Select > SelectOverlay > .option-list--option {
    padding: 0 1;
    background: $recessed;
    color: $fg2;
}
#resource-editor-fields Select > SelectOverlay > .option-list--option-highlighted {
    background: $visual;
    color: $foreground;
    text-style: bold;
}
#resource-editor-fields TextArea {
    width: 100%;
    height: 4;
    min-height: 4;
    border: none;
    background: $highlight;
}
#resource-editor-fields VaultCheckbox, #resource-policy-card VaultCheckbox {
    width: 100%;
    height: 1;
    min-height: 1;
    margin: 1 0 0 0;
    padding: 0 1;
    border: none;
    background: transparent;
}
#resource-editor-fields VaultCheckbox > .toggle--button,
#resource-policy-card VaultCheckbox > .toggle--button {
    width: 4;
    min-width: 4;
    color: $muted;
    background: transparent;
}
#resource-editor-fields VaultCheckbox.-on > .toggle--button,
#resource-policy-card VaultCheckbox.-on > .toggle--button {
    color: $ok;
    background: transparent;
    text-style: bold;
}
#resource-editor-fields VaultCheckbox > .toggle--label,
#resource-policy-card VaultCheckbox > .toggle--label {
    color: $foreground;
}
#resource-editor-fields VaultCheckbox.-on > .toggle--label,
#resource-policy-card VaultCheckbox.-on > .toggle--label {
    color: $foreground;
    text-style: bold;
}
#resource-editor-fields VaultCheckbox:focus, #resource-policy-card VaultCheckbox:focus {
    border-left: solid $accent;
    background: $highlight;
}
#resource-editor-fields VaultCheckbox:focus > .toggle--label,
#resource-policy-card VaultCheckbox:focus > .toggle--label {
    color: $foreground;
    background: transparent;
    text-style: bold;
}
#resource-editor-error {
    width: 100%;
    height: auto;
    max-height: 2;
    padding: 1 0 0 0;
    color: $bad;
}
#resource-editor-actions, #resource-policy-actions, #resource-confirm-actions {
    width: 100%;
    height: 2;
    margin: 1 0 0 0;
    align: right middle;
}
#resource-editor-actions Button, #resource-policy-actions Button, #resource-confirm-actions Button {
    width: auto;
    min-width: 11;
    height: 1;
    min-height: 1;
    margin: 0 0 0 1;
    padding: 0 2;
    border: none;
}
#resource-policy-card {
    width: 84;
    max-width: 96%;
    padding: 1 2;
}
.resource-policy-section {
    width: 100%;
    height: 1;
    margin: 0 0 1 0;
    padding: 0 1;
    color: $accent;
    text-style: bold;
    border-left: solid $accent;
}
#resource-policy-gates {
    width: 100%;
    height: 4;
    margin: 0 0 1 0;
    layout: horizontal;
    align: left top;
}
.resource-policy-gate {
    width: 1fr;
    height: 4;
    margin: 0 1 0 0;
    padding: 0 1;
    background: $base;
    border: round $rule;
}
.resource-policy-gate:last-child {
    margin-right: 0;
}
#resource-policy-card VaultCheckbox {
    width: 100%;
    height: 1;
    min-height: 1;
    margin: 0;
    padding: 0 1;
    border-left: solid transparent;
    background: transparent;
}
.resource-policy-gate-note {
    width: 100%;
    height: 1;
    padding: 0 1;
    color: $muted;
}
.resource-policy-target-copy {
    width: 1fr;
    height: 2;
    align: left middle;
}
.resource-policy-target-title {
    width: 100%;
    height: 1;
    color: $foreground;
    text-style: bold;
}
.resource-policy-row {
    width: 100%;
    height: 4;
    min-height: 4;
    margin: 0 0 1 0;
    padding: 0 0 0 1;
    background: $base;
    border: round $rule;
    align: left middle;
}
.resource-policy-row Static {
    width: 100%;
    height: 1;
    color: $fg2;
}
.resource-policy-row Button {
    width: auto;
    min-width: 9;
    height: 1;
    min-height: 1;
    margin: 0 1 0 1;
    padding: 0 2;
    border: none;
    background: $visual;
    color: $foreground;
    text-style: bold;
}
.resource-policy-row Button:focus {
    border-left: solid $accent;
    background: $highlight;
    color: $foreground;
}
#resource-policy-actions {
    height: 3;
    margin-top: 0;
}
#resource-confirm-card {
    width: 62;
}
#resource-confirm-help {
    padding-bottom: 0;
}
.short #resource-manager-help, .short #storage-manager-help, .short #proxy-manager-help, .short #justwatch-settings-help, .short #settings-group-help {
    display: block;
    max-height: 3;
    padding-left: 1;
    padding-right: 1;
}
.short #storage-manager-body {
    padding-top: 0;
}
.short #storage-preview {
    max-height: 2;
    margin-bottom: 0;
}
.short #proxy-manager-body, .short #justwatch-settings-body {
    padding-top: 0;
}
.short #proxy-preview {
    max-height: 1;
    margin-bottom: 0;
}
.short #resource-manager-actions, .short #proxy-manager-actions {
    height: 2;
    margin-top: 0;
}
.short #resource-editor-card, .short #resource-policy-card, .short #resource-confirm-card {
    max-height: 94%;
    padding-left: 1;
    padding-right: 1;
}
.short #resource-editor-card {
    height: 94%;
}
.short #resource-editor-fields {
    height: 1fr;
}
.short #resource-policy-card {
    padding-top: 0;
    padding-bottom: 0;
}
.short #resource-policy-help, .short .resource-policy-gate-note {
    display: none;
}
.short .resource-policy-section {
    margin: 0;
}
.short #resource-policy-gates {
    height: 3;
    margin: 0;
}
.short .resource-policy-gate {
    height: 3;
    padding-top: 0;
    padding-bottom: 0;
}
.short .resource-policy-row {
    height: 2;
    min-height: 2;
    margin: 0;
    padding: 0 1;
    border: none;
    border-left: solid $rule;
}
.short #resource-policy-actions {
    height: 2;
    margin: 0;
}

/* -------------------------------------------------------------- cdm picker */
#cdm-source {
    height: 2;
    margin: 0 2;
    background: $base;
}
#cdm-source Tab {
    color: $muted;
}
#cdm-source Tab.-active {
    color: $accent;
    background: $highlight;
    text-style: bold;
}
#cdm-list {
    height: 1fr;
}

/* The log's own header row: what it is on the left, what you can do with the
   whole of it on the right. */
#log-bar {
    height: 1;
}
#log-share {
    width: auto;
    margin: 0 0 0 2;
}

/* ------------------------------------------------------------ import */
#import-head {
    height: 2;
    padding: 1 2 0 2;
}
/* the folder, always in the same place: it is the answer to "where do I put
   the file", which is the question this screen exists to answer */
#import-note {
    height: 1;
    padding: 0 2;
}
#import-list {
    height: 1fr;
    padding: 0 1;
}

/* ------------------------------------------------------------ cdm rules */
#rules-head {
    height: 2;
    padding: 1 2 0 2;
}
/* one line, always there: it holds the warnings, and a row that appears and
   disappears moves the list under the cursor */
#rules-note {
    height: 1;
    padding: 0 2;
}
#rules-list {
    height: 1fr;
    padding: 0 1;
}

/* ------------------------------------------------------------ readiness */
#ready-head {
    height: 2;
    padding: 1 2 0 2;
}
#ready-body {
    height: 1fr;
    padding: 1 2;
}
/* a group heading, with room above it and none below: the rows belong to it */
.ready-group {
    height: 2;
    padding: 1 0 0 0;
}
/* auto, not 1: a missing item carries its install line and what it costs */
.ready-row {
    height: auto;
    padding: 0 0 0 1;
}

.dim { color: $dim; }
.muted { color: $muted; }
.ok { color: $ok; }
.warn { color: $warn; }
.err { color: $bad; }
.accent { color: $accent; }

/* --------------------------------------------------------------- short screen
   Everything above is written for a window with room in it: a blank row between
   rows of platforms, a field three rows tall, a footer that does not touch what
   is above it. In a twenty-row split pane that air costs the content itself - the
   platform list was squeezed to nothing - so below `ROOMY_ROWS` it is given back,
   in one place, by a class the app puts on itself when it is resized.

   Air is a luxury; the list is the screen. Same rule as the status line and the
   key bar: lose the least important thing whole. */
.short #prompt-hint {
    height: 1;
    padding: 0 1;
}
.short #filter, .short #query {
    height: 1;
    padding: 0 1;
}
.short .ask-input {
    height: 1;
    padding: 0 1;
}
.short #keybar {
    height: 1;
    padding: 0 2;
}
.short .section-head {
    height: 2;
    padding: 1 1 0 1;
}
.short .service-grid {
    grid-gutter: 0 1;
}
"""

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

from dataclasses import dataclass, replace

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
    stereo: str  # 3D video, separate from dynamic range and audio badges

    # source-language badge, deliberately separate from stream-format colours
    original: str

    # Auto search categories: quiet, separate inks, not stream/status colours
    search_services: str
    search_exports: str
    search_vault: str
    search_settings: str
    search_justwatch: str
    search_catalog: str
    search_url: str

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

    # accent scheme identity and optional multi-hue prism cycle for colourful mode
    accent_key: str = "violet"
    prism: tuple[str, ...] = ()


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
    dim="#929292",
    # the row numbers live here, and a number you are asked to type has to be as
    # readable as the name beside it - on the page and on the row under the pointer
    gutter="#8c8c8c",
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
    stereo="#91b9c5",
    original="#8eaaa4",
    search_services="#9baec2",
    search_exports="#b9aa92",
    search_vault="#9ab5a0",
    search_settings="#b4a0bb",
    search_justwatch="#b8b38f",
    search_catalog="#bd9c96",
    search_url="#96b6ba",
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
    stereo="#386477",
    original="#42665e",
    search_services="#4a6078",
    search_exports="#6b593f",
    search_vault="#4f6651",
    search_settings="#6b5273",
    search_justwatch="#635f3d",
    search_catalog="#78514b",
    search_url="#42656b",
    rule="#a9a6a0",
    bg="#f7f6f3",
    panel="#edebe6",
    # The tinted surfaces are kept pale, because whatever is drawn on them has to
    # stay readable: a selected row is a row you are about to read, and a deeper
    # tint bought a stronger selection at the cost of the text on it.
    highlight="#e7e2f6",
    hover="#e2dfd7",
    visual="#ded8f3",
    scroll_bg="#edebe6",
    scroll_fg="#98948c",
)

TOKYO_NIGHT = Palette(
    name="tokyo_night",
    dark=True,
    fg="#c0caf5",
    fg2="#a9b1d6",
    muted="#9aa5ce",
    dim="#8e98c2",
    gutter="#8a95bf",
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
    stereo="#7dcfff",
    original="#73daca",
    search_services="#9baec2",
    search_exports="#b9aa92",
    search_vault="#9ab5a0",
    search_settings="#b4a0bb",
    search_justwatch="#b8b38f",
    search_catalog="#bd9c96",
    search_url="#96b6ba",
    rule="#414868",
    bg="#1a1b26",
    panel="#1f2335",
    highlight="#262b3d",
    hover="#31374f",
    visual="#3b4261",
    scroll_bg="#16161e",
    scroll_fg="#565f89",
)

CATPPUCCIN = Palette(
    name="catppuccin",
    dark=True,
    fg="#cdd6f4",
    fg2="#bac2de",
    muted="#a6adc8",
    dim="#969cb5",
    gutter="#9197b0",
    accent="#cba6f7",
    manifest="#89b4fa",
    ok="#a6e3a1",
    warn="#f9e2af",
    error="#f38ba8",
    dv="#cba6f7",
    hdr10="#f9e2af",
    hdr10plus="#fab387",
    hlg="#89b4fa",
    hdr_vivid="#89dceb",
    atmos="#a6e3a1",
    audio_vivid="#f5c2e7",
    stereo="#74c7ec",
    original="#94e2d5",
    search_services="#94b0d6",
    search_exports="#cbb89e",
    search_vault="#9bc4a4",
    search_settings="#bfa6cc",
    search_justwatch="#c9c099",
    search_catalog="#cda3a6",
    search_url="#93c1c4",
    rule="#585b70",
    bg="#1e1e2e",
    panel="#181825",
    highlight="#2b2c3d",
    hover="#36384d",
    visual="#45475a",
    scroll_bg="#11111b",
    scroll_fg="#6c7086",
)

NORD = Palette(
    name="nord",
    dark=True,
    fg="#eceff4",
    fg2="#d8dee9",
    muted="#aeb8ca",
    dim="#9ca8be",
    gutter="#96a3ba",
    accent="#88c0d0",
    manifest="#81a1c1",
    ok="#a3be8c",
    warn="#ebcb8b",
    error="#ee8490",
    dv="#b48ead",
    hdr10="#ebcb8b",
    hdr10plus="#e39d7a",
    hlg="#81a1c1",
    hdr_vivid="#88c0d0",
    atmos="#a3be8c",
    audio_vivid="#cfa3c7",
    stereo="#8fbcbb",
    original="#8fbcbb",
    search_services="#9bb4ce",
    search_exports="#c5b49b",
    search_vault="#a3be8c",
    search_settings="#b89eb3",
    search_justwatch="#d2be96",
    search_catalog="#c99ca2",
    search_url="#8fbcbb",
    rule="#4c566a",
    bg="#21262f",
    panel="#272d38",
    highlight="#2e3440",
    hover="#3b4252",
    visual="#434c5e",
    scroll_bg="#1b2028",
    scroll_fg="#616e88",
)

GRUVBOX = Palette(
    name="gruvbox",
    dark=True,
    fg="#ebdbb2",
    fg2="#d5c4a1",
    muted="#bdae93",
    dim="#a89984",
    gutter="#a3947f",
    accent="#fabd2f",
    manifest="#83a598",
    ok="#b8bb26",
    warn="#fabd2f",
    error="#fb6b58",
    dv="#d3869b",
    hdr10="#fabd2f",
    hdr10plus="#fe8019",
    hlg="#83a598",
    hdr_vivid="#8ec07c",
    atmos="#b8bb26",
    audio_vivid="#d3869b",
    stereo="#83a598",
    original="#8ec07c",
    search_services="#9ab4a7",
    search_exports="#d5b884",
    search_vault="#b8bb6a",
    search_settings="#cfa0ae",
    search_justwatch="#dec07c",
    search_catalog="#df9789",
    search_url="#8ec07c",
    rule="#5a524c",
    bg="#1d2021",
    panel="#262524",
    highlight="#2f2d2b",
    hover="#3c3836",
    visual="#504945",
    scroll_bg="#181a1b",
    scroll_fg="#665c54",
)

HIGH_CONTRAST = Palette(
    name="high_contrast",
    dark=True,
    fg="#ffffff",
    fg2="#f0f0f0",
    muted="#d0d0d0",
    dim="#bdbdbd",
    gutter="#b3b3b3",
    accent="#5ce1ff",
    manifest="#75baff",
    ok="#5df27a",
    warn="#ffd84d",
    error="#ff6b7a",
    dv="#d6a6ff",
    hdr10="#ffd84d",
    hdr10plus="#ffa657",
    hlg="#75baff",
    hdr_vivid="#5ce1ff",
    atmos="#5df27a",
    audio_vivid="#ff94d6",
    stereo="#7ae6ff",
    original="#7ef2d4",
    search_services="#9ecaff",
    search_exports="#e6cc9e",
    search_vault="#8ef0a2",
    search_settings="#deb0ff",
    search_justwatch="#f0e08e",
    search_catalog="#ffadb5",
    search_url="#8be9f5",
    rule="#8c8c8c",
    bg="#000000",
    panel="#0c0c0c",
    highlight="#1a1a1a",
    hover="#262626",
    visual="#333333",
    scroll_bg="#000000",
    scroll_fg="#9e9e9e",
)

PALETTES: dict[str, Palette] = {
    "dark": DARK,
    "light": LIGHT,
    "tokyo_night": TOKYO_NIGHT,
    "catppuccin": CATPPUCCIN,
    "nord": NORD,
    "gruvbox": GRUVBOX,
    "high_contrast": HIGH_CONTRAST,
}

DEFAULT_THEME = "dark"
DEFAULT_ACCENT = "violet"
DEFAULT_TITLE_COLOR = "accent"
DEFAULT_DENSITY = "auto"
DENSITY_OPTIONS: tuple[str, ...] = ("auto", "comfortable", "compact")
DEFAULT_SEMANTIC_COLORS = "default"
SEMANTIC_COLOR_OPTIONS: tuple[str, ...] = ("default", "colorblind")

#: Okabe-Ito inspired colorblind-safe status colors (bluish-teal / amber / magenta)
#: tuned for >= 4.5:1 WCAG contrast on every dark and light surface.
_COLORBLIND_DARK_SEMANTICS: dict[str, str] = {
    "ok": "#4fd1b5",
    "warn": "#f5be41",
    "error": "#e58ac4",
}
_COLORBLIND_LIGHT_SEMANTICS: dict[str, str] = {
    "ok": "#007358",
    "warn": "#8f5400",
    "error": "#9e2a76",
}


@dataclass(frozen=True)
class AccentPreset:
    """One system accent scheme, tuned separately for dark and light surfaces."""

    key: str
    dark_accent: str
    light_accent: str
    light_highlight: str
    light_visual: str
    colorful: bool = False
    dark_prism: tuple[str, ...] = ()
    light_prism: tuple[str, ...] = ()

    def accent_for(self, dark: bool) -> str:
        return self.dark_accent if dark else self.light_accent

    def swatch_stops(self, dark: bool) -> tuple[str, ...]:
        if self.colorful:
            return self.dark_prism if dark else self.light_prism
        return (self.accent_for(dark),)


@dataclass(frozen=True)
class TitleColorPreset:
    """One solid colour or horizontal gradient for block-letter titles."""

    key: str
    dark_stops: tuple[str, ...]
    light_stops: tuple[str, ...]

    def stops_for(self, dark: bool) -> tuple[str, ...]:
        return self.dark_stops if dark else self.light_stops


_DARK_RAINBOW: tuple[str, ...] = (
    "#f7768e",
    "#ff9e64",
    "#e0af68",
    "#73d88c",
    "#2ac3de",
    "#7aa2f7",
    "#bb9af7",
)
_LIGHT_RAINBOW: tuple[str, ...] = (
    "#b52348",
    "#a33b0a",
    "#8a5200",
    "#1d6f35",
    "#0b6e82",
    "#1d4ed8",
    "#5b3aa6",
)

#: System accent presets. Every value maintains >= 4.7:1 WCAG contrast on both
#: dark and light surfaces, including light-mode tinted highlight/visual rows.
ACCENT_PRESETS: dict[str, AccentPreset] = {
    "violet": AccentPreset(
        key="violet",
        dark_accent=DARK.accent,
        light_accent=LIGHT.accent,
        light_highlight=LIGHT.highlight,
        light_visual=LIGHT.visual,
    ),
    "colorful": AccentPreset(
        key="colorful",
        dark_accent="#4fd6be",
        light_accent="#0e6f73",
        light_highlight="#dff0ef",
        light_visual="#d3eae8",
        colorful=True,
        dark_prism=("#4fd6be", "#bb9af7", "#73d88c", "#e0af68", "#f7768e", "#7aa2f7"),
        light_prism=("#0e6f73", "#5b3aa6", "#1d6f35", "#8a5200", "#ad1f43", "#1d4ed8"),
    ),
    "blue": AccentPreset(
        key="blue",
        dark_accent="#7aa2f7",
        light_accent="#1d4ed8",
        light_highlight="#e2eaf9",
        light_visual="#d5e2f7",
    ),
    "cyan": AccentPreset(
        key="cyan",
        dark_accent="#2ac3de",
        light_accent="#0b6e82",
        light_highlight="#e0f0f4",
        light_visual="#d3eaf0",
    ),
    "green": AccentPreset(
        key="green",
        dark_accent="#73d88c",
        light_accent="#1d6f35",
        light_highlight="#e2f1e6",
        light_visual="#d4ebd9",
    ),
    "amber": AccentPreset(
        key="amber",
        dark_accent="#e0af68",
        light_accent="#8a5200",
        light_highlight="#f3ebd9",
        light_visual="#ece0c6",
    ),
    "orange": AccentPreset(
        key="orange",
        dark_accent="#ff9e64",
        light_accent="#a33b0a",
        light_highlight="#f6e7df",
        light_visual="#f0dbd0",
    ),
    "rose": AccentPreset(
        key="rose",
        dark_accent="#f7768e",
        light_accent="#ad1f43",
        light_highlight="#f6e2e8",
        light_visual="#f0d4dc",
    ),
    "mono": AccentPreset(
        key="mono",
        dark_accent="#d6d6d6",
        light_accent="#2f333b",
        light_highlight="#e6e6e4",
        light_visual="#dcdcd9",
    ),
}

#: Title/banner colour presets (solid hues and multi-stop gradients).
#: ``"accent"`` is handled dynamically by :func:`title_color_stops`.
TITLE_COLOR_PRESETS: dict[str, TitleColorPreset] = {
    "rainbow": TitleColorPreset("rainbow", _DARK_RAINBOW, _LIGHT_RAINBOW),
    "aurora": TitleColorPreset(
        "aurora",
        ("#73d88c", "#4fd6be", "#2ac3de", "#7aa2f7", "#bb9af7"),
        ("#1d6f35", "#0e6f73", "#0b6e82", "#1d4ed8", "#5b3aa6"),
    ),
    "sunset": TitleColorPreset(
        "sunset",
        ("#f7768e", "#ff9e64", "#e0af68"),
        ("#ad1f43", "#a33b0a", "#8a5200"),
    ),
    "ocean": TitleColorPreset(
        "ocean",
        ("#4fd6be", "#2ac3de", "#7aa2f7", "#9d7cd8"),
        ("#0e6f73", "#0b6e82", "#1d4ed8", "#5b3aa6"),
    ),
    "violet": TitleColorPreset("violet", ("#bb9af7",), ("#5b3aa6",)),
    "blue": TitleColorPreset("blue", ("#7aa2f7",), ("#1d4ed8",)),
    "cyan": TitleColorPreset("cyan", ("#2ac3de",), ("#0b6e82",)),
    "green": TitleColorPreset("green", ("#73d88c",), ("#1d6f35",)),
    "amber": TitleColorPreset("amber", ("#e0af68",), ("#8a5200",)),
    "orange": TitleColorPreset("orange", ("#ff9e64",), ("#a33b0a",)),
    "rose": TitleColorPreset("rose", ("#f7768e",), ("#ad1f43",)),
    "mono": TitleColorPreset("mono", ("#d6d6d6",), ("#2f333b",)),
}


def normalize_theme(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return key if key in PALETTES else DEFAULT_THEME


def normalize_density(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return key if key in DENSITY_OPTIONS else DEFAULT_DENSITY


def normalize_semantic_colors(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return key if key in SEMANTIC_COLOR_OPTIONS else DEFAULT_SEMANTIC_COLORS


def normalize_accent(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return key if key in ACCENT_PRESETS else DEFAULT_ACCENT


def normalize_title_color(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if key == DEFAULT_TITLE_COLOR or key in TITLE_COLOR_PRESETS:
        return key
    return DEFAULT_TITLE_COLOR


def theme_name_for(
    mode: str,
    accent: str = DEFAULT_ACCENT,
    semantic: str = DEFAULT_SEMANTIC_COLORS,
) -> str:
    base = normalize_theme(mode)
    accent_key = normalize_accent(accent)
    semantic_key = normalize_semantic_colors(semantic)
    name = base if accent_key == DEFAULT_ACCENT else f"{base}-{accent_key}"
    return f"{name}-cb" if semantic_key == "colorblind" else name


def palette_for(
    mode: str,
    accent: str = DEFAULT_ACCENT,
    semantic: str = DEFAULT_SEMANTIC_COLORS,
) -> Palette:
    """The palette for a theme mode, accent preset, and semantic mode."""
    base_name = normalize_theme(mode)
    base = PALETTES.get(base_name, DARK)
    accent_key = normalize_accent(accent)
    semantic_key = normalize_semantic_colors(semantic)
    if accent_key == DEFAULT_ACCENT and semantic_key == DEFAULT_SEMANTIC_COLORS:
        return base
    result = base
    if accent_key != DEFAULT_ACCENT:
        preset = ACCENT_PRESETS[accent_key]
        if result.dark:
            result = replace(
                result,
                accent=preset.dark_accent,
                accent_key=accent_key,
                prism=preset.dark_prism if preset.colorful else (),
            )
        else:
            result = replace(
                result,
                accent=preset.light_accent,
                highlight=preset.light_highlight,
                visual=preset.light_visual,
                accent_key=accent_key,
                prism=preset.light_prism if preset.colorful else (),
            )
    if semantic_key == "colorblind":
        overrides = _COLORBLIND_DARK_SEMANTICS if result.dark else _COLORBLIND_LIGHT_SEMANTICS
        result = replace(result, **overrides)
    return replace(result, name=theme_name_for(base_name, accent_key, semantic_key))


def title_color_stops(
    title_color: str | None,
    *,
    dark: bool,
    accent: str = DEFAULT_ACCENT,
) -> tuple[str, ...]:
    """Concrete hex stop(s) for rendering the ``unidl`` and service titles."""
    mode = normalize_title_color(title_color)
    accent_key = normalize_accent(accent)
    if mode == DEFAULT_TITLE_COLOR:
        preset = ACCENT_PRESETS[accent_key]
        if preset.colorful:
            return TITLE_COLOR_PRESETS["rainbow"].stops_for(dark)
        return (preset.accent_for(dark),)
    return TITLE_COLOR_PRESETS[mode].stops_for(dark)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    cleaned = color.lstrip("#")
    return int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)


def interpolate_stops(stops: tuple[str, ...], position: float) -> str:
    """Linearly interpolate an RGB hex colour along ``stops`` for ``0.0 <= position <= 1.0``."""
    if not stops:
        return DARK.accent
    if len(stops) == 1:
        return stops[0]
    clamped = max(0.0, min(1.0, float(position)))
    scaled = clamped * (len(stops) - 1)
    low = int(scaled)
    high = min(len(stops) - 1, low + 1)
    frac = scaled - low
    r1, g1, b1 = _hex_to_rgb(stops[low])
    r2, g2, b2 = _hex_to_rgb(stops[high])
    r = round(r1 + (r2 - r1) * frac)
    g = round(g1 + (g2 - g1) * frac)
    b = round(b1 + (b2 - b1) * frac)
    return f"#{r:02x}{g:02x}{b:02x}"


def swatch_markup(stops: tuple[str, ...], *, blocks: int = 4) -> str:
    """Fixed-width Rich markup colour chip for option rows and settings summaries."""
    if not stops:
        return ""
    count = max(2, blocks)
    if len(stops) == 1:
        return f"[{stops[0]}]{'━' * count}[/]"
    parts = [
        f"[{interpolate_stops(stops, idx / max(1, count - 1))}]━[/]"
        for idx in range(count)
    ]
    return "".join(parts)


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
            "stereo": palette.stereo,
            "original": palette.original,
            "search-services": palette.search_services,
            "search-exports": palette.search_exports,
            "search-vault": palette.search_vault,
            "search-settings": palette.search_settings,
            "search-justwatch": palette.search_justwatch,
            "search-catalog": palette.search_catalog,
            "search-url": palette.search_url,
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

#: Every (mode, accent) Textual theme registered at startup so switching accent
#: or live-previewing options repaints without dynamic stylesheet rebuilds.
ALL_THEMES: dict[str, Theme] = {
    theme_name_for(mode, accent): textual_theme(palette_for(mode, accent))
    for mode in PALETTES
    for accent in ACCENT_PRESETS
}

#: mode -> the registered Textual theme name (default accent)
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
    background: $visual;
    color: $foreground;
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
#chrome.tight .chrome-button {
    padding: 0 1;
    margin: 0 1 0 0;
}
#chrome.compact .chrome-button {
    padding: 0 1;
    margin: 0;
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
#search-statusline {
    width: 1fr;
    height: 1;
    /* StatusChip contributes two cells of its own padding.  One cell here
       makes its visible text start at the same column as #masthead's text. */
    padding: 0 1;
    align: left middle;
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
    overflow-x: hidden;
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
    background: $base 75%;
}
#chapter-modal {
    width: 88;
    max-width: 94%;
    height: auto;
    max-height: 82%;
    background: $recessed;
    border: round $rule;
    padding: 1 2;
}
#chapter-modal-head {
    height: 1;
    padding: 0 1;
}
#chapter-modal-title {
    width: 1fr;
    height: 1;
    color: $accent;
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
    background: $base 75%;
}
#lyrics-modal {
    width: 88;
    max-width: 94%;
    height: auto;
    max-height: 82%;
    background: $recessed;
    border: round $rule;
    padding: 1 2;
}
#lyrics-modal-head {
    height: 1;
    padding: 0 1;
}
#lyrics-modal-title {
    width: 1fr;
    height: 1;
    color: $accent;
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
AttachmentsScreen {
    align: center middle;
    background: $base 75%;
}
#attachment-modal {
    width: 72;
    max-width: 94%;
    height: auto;
    max-height: 86%;
    background: $recessed;
    border: round $rule;
    padding: 1 2;
}
#attachment-modal-head {
    height: 1;
    padding: 0 1;
}
#attachment-modal-title {
    width: 1fr;
    height: 1;
    overflow-x: hidden;
    color: $accent;
    text-style: bold;
}
#attachment-modal-close {
    width: 5;
    height: 1;
    content-align: right middle;
    color: $accent;
    text-style: bold;
}
#attachment-modal-close:hover {
    background: $highlight;
    color: $foreground;
}
#attachment-summary, #attachment-info {
    height: auto;
    padding: 1 2 0 2;
    color: $muted;
    text-align: center;
}
#attachment-image {
    width: 1fr;
    height: 1fr;
    min-height: 8;
    max-height: 28;
    content-align: center middle;
    padding: 1;
}
#attachment-modal-hint {
    height: 1;
    padding: 0 2;
    color: $muted;
    text-align: center;
}
#attachment-modal-nav, #chapter-modal-nav, #lyrics-modal-nav {
    width: 1fr;
    height: 1;
    margin: 1 0;
    padding: 0 2;
    align: center middle;
}
#attachment-modal-nav Button, #chapter-modal-nav Button, #lyrics-modal-nav Button {
    width: auto;
    min-width: 0;
    height: 1;
    padding: 0 1;
    margin: 0 1;
    border: none;
    background: transparent;
    background-tint: transparent;
    color: $accent;
    text-style: none;
}
#attachment-modal-nav Button:hover, #chapter-modal-nav Button:hover, #lyrics-modal-nav Button:hover {
    border: none;
    background: $highlight;
    color: $foreground;
}
#attachment-modal-nav Button:focus, #chapter-modal-nav Button:focus, #lyrics-modal-nav Button:focus {
    text-style: underline;
}
#attachment-modal-nav Button:disabled, #chapter-modal-nav Button:disabled, #lyrics-modal-nav Button:disabled {
    border: none;
    background: transparent;
    color: $muted;
    text-style: none;
}
.ask-title-row {
    height: 1;
    width: 1fr;
    padding: 0;
}
.ask-title-row .ask-title {
    width: 1fr;
    max-width: 1fr;
    overflow-x: hidden;
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
.audio-track-controls .ask-hint {
    /* One status line is enough; shared hint padding can consume the only
       row available to a short audio batch picker. */
    height: 1;
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
#queue-summary-row {
    display: none;
    height: 2;
    padding: 1 2 0 2;
}
#queue-summary {
    display: none;
    width: 1fr;
    height: 1;
    padding: 0;
}
#queue-retry-all {
    display: none;
    color: $warn;
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
.queue-row.retryable {
    color: $warn;
}
.queue-row.retryable:hover {
    background: $highlight;
    color: $foreground;
    text-style: underline;
}

/* Batch inspection follows the same compact, scrollable card as chapters. */
JobDetailsScreen {
    align: center middle;
    background: $base 75%;
}
#job-modal {
    width: 100;
    max-width: 94%;
    height: 82%;
    background: $recessed;
    border: round $rule;
    padding: 1 2;
}
#job-modal-head {
    height: 1;
    padding: 0 1;
}
#job-modal-title {
    width: 1fr;
    height: 1;
}
#job-modal-close {
    width: 5;
    height: 1;
    content-align: right middle;
    color: $accent;
}
#job-modal-close:hover {
    background: $highlight;
}
#job-modal-scroll {
    height: 1fr;
    padding: 1;
    overflow-x: hidden;
    scrollbar-size-horizontal: 0;
}
#job-modal-body {
    height: auto;
    width: 1fr;
    color: $foreground;
}
#job-modal-hint {
    height: auto;
    padding: 0 1;
    color: $muted;
}
#job-modal-nav {
    height: 1;
    align: center middle;
    margin: 1 0;
}
#job-modal-nav Button {
    width: auto;
    min-width: 0;
    height: 1;
    padding: 0 1;
    margin: 0 1;
    border: none;
    background: transparent;
    background-tint: transparent;
    color: $accent;
}
#job-modal-nav Button:hover, #job-modal-nav Button:focus {
    background: $highlight;
    text-style: underline;
}
#job-modal-nav Button:disabled {
    color: $muted;
}
DownloadScreen.active-ask #queue-list {
    max-height: 4;
}
DownloadScreen.active-ask MultiPickWidget .ask-hint {
    height: 1;
    padding: 0 1;
}
.short #job-modal {
    height: 90%;
    padding: 0 1;
}
.short #job-modal-nav {
    margin: 0;
}

DownloadScreen #ask-area {
    height: auto;
    max-height: 20%;
}

/* A queued title may ask for tracks after the preceding title finishes. Let
   the picker use the flexible remainder and scroll internally on small windows. */
DownloadScreen #ask-area.active-ask {
    height: 1fr;
    min-height: 5;
    max-height: 100%;
}

/* These panels describe the previous/current transfer and are stale while a
   queued title's track selection is waiting for input. */
DownloadScreen.active-ask #delivery-details {
    display: none;
}

DownloadScreen.active-ask #delivery-audio-side {
    display: none;
}

#statusline {
    width: 1fr;
    height: 1;
    padding: 0 2;
    align: left middle;
}
/* Only the home CDM / DRM / delivery / config row is centered. Page hints
   and search statistics follow their headings' left edge. */
HomeScreen #statusline {
    align: center middle;
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
#vault-add-card {
    width: 92;
    max-width: 96%;
    height: 90%;
    max-height: 36;
    padding: 1 2 0 2;
    background: $recessed;
    border: round $rule;
}
AddKeysScreen .modal-head {
    margin: 0;
}
AddKeysScreen #masthead {
    width: 1fr;
    height: 1;
    padding: 0;
    color: $accent;
    text-style: bold;
}
AddKeysScreen #subhead {
    width: 100%;
    height: auto;
    max-height: 2;
    padding: 0 0 1 0;
    color: $muted;
}
#vault-form-scroll {
    width: 100%;
    height: 1fr;
    min-height: 4;
    padding: 0 1;
    background: $base;
    border: round $rule;
    overflow-x: hidden;
    scrollbar-size: 1 1;
}
#vault-add-metadata {
    height: auto;
    min-height: 3;
}
#vault-service-field, #vault-title-field {
    height: auto;
    width: 1fr;
    min-width: 0;
}
#vault-service-field { margin-right: 2; }
AddKeysScreen.narrow #vault-add-metadata { layout: vertical; }
AddKeysScreen.narrow #vault-service-field { margin-right: 0; }
.vault-field-label {
    width: 100%;
    height: 1;
    margin: 1 0 0 0;
    color: $muted;
}
#vault-add-metadata .vault-field-label {
    margin: 0;
}
#vault-service-query, #vault-title {
    width: 100%;
    height: 1;
    padding: 0 1;
    border: none;
    border-left: solid $rule;
    background: $highlight;
}
#vault-service-query:focus, #vault-title:focus {
    border-left: solid $accent;
    background: $visual;
}
#vault-service-state {
    width: 100%;
    height: auto;
    max-height: 2;
    padding: 0 1;
    color: $dim;
}
#vault-service-list {
    width: 100%;
    height: 4;
    border: none;
    border-left: solid $rule;
    background: $recessed;
    overflow-x: hidden;
    scrollbar-size: 1 1;
}
#vault-service-list:focus {
    border-left: solid $accent;
}
#vault-service-list > .option-list--option-highlighted {
    background: $visual;
    color: $foreground;
    text-style: bold;
}
#vault-keys-head {
    width: 100%;
    height: 1;
    margin: 1 0 0 0;
}
#vault-keys-head .vault-field-label {
    width: 1fr;
    margin: 0;
}
#vault-keys-count {
    width: auto;
    height: 1;
    padding: 0 1 0 0;
    color: $ok;
    text-style: bold;
}
#vault-keys-help {
    width: 100%;
    height: auto;
    max-height: 2;
    margin-bottom: 0;
    color: $dim;
}
#vault-pairs {
    width: 100%;
    height: 9;
    min-height: 5;
    border: none;
    border-left: solid $rule;
    padding: 0 1;
    background: $recessed;
    color: $foreground;
}
#vault-pairs:focus {
    border-left: solid $accent;
    background: $highlight;
}
#vault-validation {
    display: none;
    width: 100%;
    height: auto;
    margin: 1 0 0 0;
    padding: 0 1;
    background: $recessed;
    border-left: solid $rule;
    color: $dim;
}
#vault-validation.warning {
    border-left: solid $warn;
    color: $warn;
}
#vault-validation.error {
    border-left: solid $bad;
    color: $bad;
}
#vault-destination-row {
    width: 100%;
    height: auto;
    min-height: 2;
    max-height: 3;
    margin: 1 0 0 0;
    padding: 0 1;
    background: $base;
    border-left: solid $rule;
    align-vertical: middle;
}
#vault-destination-copy {
    width: 1fr;
    height: auto;
}
#vault-destination-copy .vault-field-label {
    margin: 0;
    color: $dim;
}
#vault-destination-state {
    width: 1fr;
    height: auto;
    max-height: 2;
    padding: 0;
    color: $foreground;
}
#vault-destination-choose {
    width: auto;
    min-width: 10;
    height: 1;
    min-height: 1;
    margin: 0 0 0 1;
    padding: 0 2;
    border: none;
    background: $recessed;
    color: $fg2;
}
#vault-destination-choose:hover, #vault-destination-choose:focus {
    background: $visual;
    color: $foreground;
    text-style: bold;
}
#vault-add-actions {
    width: 100%;
    height: 1;
    margin: 1 0;
    align: right middle;
}
#vault-action-hint {
    width: 1fr;
    height: 1;
    color: $dim;
}
#vault-add-actions Button {
    width: auto;
    min-width: 10;
    height: 1;
    min-height: 1;
    margin: 0 0 0 1;
    padding: 0 2;
    border: none;
}
AddKeysScreen.compact #vault-add-card {
    height: 94%;
    max-height: 94%;
    padding: 0 1;
}
AddKeysScreen.compact .modal-head { margin: 0; }
AddKeysScreen.compact #subhead { padding: 0; max-height: 1; }
AddKeysScreen.compact #vault-pairs { height: 6; }
AddKeysScreen.compact .vault-field-label,
AddKeysScreen.compact #vault-keys-head { margin: 0; }
AddKeysScreen.compact #vault-destination-row { margin: 0; }
AddKeysScreen.compact #vault-add-actions { height: 1; margin: 0; }
AddKeysScreen.narrow #vault-add-card { padding-left: 1; padding-right: 1; }
AddKeysScreen.narrow #vault-action-hint { display: none; }
AddKeysScreen.narrow #vault-add-actions Button,
AddKeysScreen.narrow #vault-destination-choose { min-width: 6; padding: 0 1; }

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
#recent-bar {
    display: none;
    width: 100%;
    height: 1;
    padding: 0 1;
    overflow-x: hidden;
}
.recent-label {
    width: auto;
    height: 1;
    margin: 0 1 0 0;
    color: $muted;
}
.recent-chip {
    width: auto;
    height: 1;
    padding: 0 1;
    margin: 0 1 0 0;
    background: $recessed;
    color: $fg2;
}
.recent-chip:hover {
    background: $highlight;
    color: $accent;
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
#setup-notes {
    height: auto;
    max-height: 40%;
    overflow-x: hidden;
}
#setup-note, #service-setup-note, #download-setup-note {
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
#setup-note:hover, #service-setup-note:hover, #download-setup-note:hover {
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

/* ------------------------------------------------------- experimental home
   Keep these rules in the app stylesheet, not widget DEFAULT_CSS: app-wide
   Input/focus and OptionList rules otherwise override the search layout. */
BetaHomeScreen #beta-body {
    height: 1fr;
    padding: 1 2 0 2;
}
BetaHomeScreen #setup-notes { width: 100%; max-height: 5; }
BetaHomeScreen #beta-content { width: 1fr; height: 1fr; align: center top; }
BetaHomeScreen #beta-column { width: 100%; height: 1fr; }
BetaHomeScreen #beta-heading {
    height: 1;
    margin: 1 0;
    color: $muted;
    background: transparent;
}
BetaHomeScreen #beta-card {
    width: 100%;
    /* A single text row needs an odd-height outline to be truly centred.
       Keep the extra breathing room outside, not below the text inside. */
    height: 3;
    margin-bottom: 1;
    border: round $rule;
    background: $recessed;
    padding: 0 1;
    align-vertical: middle;
}
BetaHomeScreen #beta-mode, BetaHomeScreen #beta-submit {
    height: 1;
    min-height: 1;
    width: auto;
    min-width: 6;
    border: none;
    padding: 0 1;
    margin: 0;
    background: transparent;
    background-tint: transparent;
    tint: transparent;
    color: $accent;
    text-style: bold;
    content-align: center middle;
    text-wrap: nowrap;
    text-overflow: ellipsis;
}
BetaHomeScreen #beta-mode { max-width: 32; border-right: solid $rule; }
BetaHomeScreen #beta-submit { border-left: solid $rule; }
BetaHomeScreen #beta-clear {
    width: 3;
    min-width: 3;
    height: 1;
    min-height: 1;
    padding: 0;
    margin: 0;
    border: none;
    background: transparent;
    background-tint: transparent;
    tint: transparent;
    color: $muted;
    content-align: center middle;
    visibility: hidden;
}
BetaHomeScreen #beta-clear.has-query { visibility: visible; }
BetaHomeScreen #beta-clear:hover, BetaHomeScreen #beta-clear:focus {
    background: $highlight;
    color: $foreground;
    tint: transparent;
}
BetaHomeScreen #beta-mode:hover, BetaHomeScreen #beta-submit:hover,
BetaHomeScreen #beta-mode:focus, BetaHomeScreen #beta-submit:focus {
    background: $highlight;
    tint: transparent;
}
BetaHomeScreen #beta-card #filter, BetaHomeScreen #beta-card #filter:focus {
    width: 1fr;
    min-width: 6;
    height: 1;
    min-height: 1;
    border: none;
    padding: 0 2;
    margin: 0;
    background: transparent;
    background-tint: transparent;
}
BetaHomeScreen #filter > .input--placeholder {
    color: $muted;
    text-style: none;
}
BetaHomeScreen #beta-hint {
    height: 1;
    margin-top: 1;
    color: $dim;
    background: transparent;
}
BetaHomeScreen #recent-bar {
    margin-top: 1;
    padding: 0;
}
BetaHomeScreen.compact #recent-bar {
    margin-top: 0;
}
BetaHomeScreen #beta-summary, BetaHomeScreen #beta-empty {
    height: auto;
    margin-top: 1;
    color: $muted;
    background: transparent;
}
BetaHomeScreen #beta-summary { height: 1; }
BetaHomeScreen #beta-results, BetaHomeScreen #beta-results:focus {
    width: 100%;
    height: 1fr;
    min-height: 1;
    background: transparent;
    background-tint: transparent;
    border: none;
    padding: 0;
}
BetaHomeScreen.compact #beta-heading, BetaHomeScreen.compact #beta-hint { display: none; }
BetaHomeScreen.compact #beta-card { margin-bottom: 0; }
BetaHomeScreen.compact #setup-notes { max-height: 3; }
BetaHomeScreen.compact #setup-note,
BetaHomeScreen.compact #service-setup-note,
BetaHomeScreen.compact #download-setup-note { margin: 0; }
BetaHomeScreen.narrow #beta-mode { max-width: 18; }
BetaHomeScreen.narrow #beta-card #filter,
BetaHomeScreen.narrow #beta-card #filter:focus { padding-left: 1; padding-right: 1; }

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
    overflow-x: hidden;
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
#multi-summary {
    display: none;
    height: 1;
    padding: 0 1;
    background: $recessed;
    color: $fg2;
    overflow: hidden;
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
/* Half-block QR is one module per column. Grow with the code rather than
   wrapping modules, which would break the finder/timing grid a camera needs. */
.manual-card.qr-card {
    width: 78;
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
    text-style: none;
    text-wrap: nowrap;
}
.qr-render-error {
    width: 100%;
    height: auto;
    color: $warn;
    text-align: center;
}
.qr-open-image {
    width: auto;
    min-width: 0;
    height: 1;
    padding: 0 1;
    margin: 1 0 0 0;
    border: none;
    background: $highlight;
    color: $manifest;
}
.qr-open-image:hover {
    background: $visual;
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
.chip-static:hover, .pill.chip-static:hover {
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
#log-pane.collapsed .log-tail,
#log-pane.collapsed #log-search-bar {
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
#log-pane.delivery .log-tail {
    height: auto;
    /* Ten rows minus the border, padding and header leaves seven for text. */
    max-height: 7;
}
#log-pane.delivery RichLog {
    height: auto;
    max-height: 7;
}
/* The delivery selector has the same specificity as the state selectors above;
   keep these explicit overrides after it so a live job can resize the pane too. */
#log-pane.delivery.tall {
    height: 1fr;
    max-height: 100h;
}
#log-pane.delivery.tall .log-tail, #log-pane.delivery.tall RichLog {
    height: 1fr;
    max-height: 100h;
}
#log-pane.delivery.collapsed {
    height: 3;
}
#log-pane.delivery.collapsed .log-tail {
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
    background: $base 75%;
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
.modal-head > .modal-close, .modal-close {
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
#about-modal, #update-modal, #shortcuts-modal {
    width: 86;
    max-width: 94%;
    height: auto;
    max-height: 84%;
    padding: 1 2;
    background: $recessed;
    border: round $rule;
}
#about-modal-head, #update-modal-head, #shortcuts-modal-head {
    width: 100%;
    height: 2;
}
#about-modal-title, #update-modal-title, #shortcuts-modal-title {
    width: 1fr;
    height: 1;
    color: $accent;
    text-style: bold;
}
#about-modal-close, #update-modal-close, #shortcuts-modal-close {
    width: 5;
    height: 1;
    content-align: right middle;
    color: $accent;
    text-style: bold;
}
#about-modal-close:hover, #update-modal-close:hover, #shortcuts-modal-close:hover {
    background: $highlight;
    color: $foreground;
}
#about-intro, #about-license-note, #update-status, #update-result {
    width: 100%;
    height: auto;
    padding: 0 0 1 0;
    color: $muted;
}
#about-list, #shortcuts-list {
    width: 100%;
    height: 1fr;
    min-height: 8;
    max-height: 28;
    padding: 0 1;
    background: $base;
}
#shortcuts-body {
    width: 100%;
    height: auto;
    padding: 1 0;
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
#about-modal-hint, #update-modal-hint, #shortcuts-modal-hint {
    width: 100%;
    height: 2;
    padding: 1 0 0 0;
    color: $dim;
}
#modal-card {
    width: 78;
    max-width: 94%;
    height: auto;
    max-height: 86%;
    padding: 1 2;
    background: $recessed;
    border: round $rule;
}
.service-policy-card {
    width: 84;
    max-width: 94%;
    height: auto;
    max-height: 90%;
    padding: 1 2;
    background: $recessed;
    border: round $rule;
}
.modal-card-title, .proxy-editor-title {
    width: 1fr;
    height: 1;
    padding: 0;
    color: $accent;
    text-style: bold;
}
.modal-card-help, .proxy-editor-help {
    width: 100%;
    height: auto;
    padding: 0 0 1 0;
    color: $muted;
}
#modal-card OptionList, .service-policy-card OptionList {
    height: auto;
    min-height: 3;
    max-height: 20;
    background: $base;
    border: round $rule;
}
#modal-card.modal-card-flex {
    height: 86%;
}
#modal-card.appearance-modal {
    max-height: 92%;
}
#modal-card.appearance-modal.modal-card-flex {
    height: 92%;
}
.service-policy-card.modal-card-flex,
.proxy-editor-card.modal-card-flex {
    height: 90%;
}
#vault-target-card.modal-card-flex {
    height: 82%;
}
#modal-card.modal-card-flex OptionList,
.service-policy-card.modal-card-flex OptionList,
#vault-target-card.modal-card-flex #vault-target-list,
.proxy-editor-card.modal-card-flex .proxy-editor-fields,
.proxy-editor-card.modal-card-flex #proxy-provider-types {
    height: 1fr;
    max-height: 100%;
}
#modal-card Input {
    width: 100%;
    height: 1;
    padding: 0 1;
    border: none;
    border-left: solid $rule;
    background: $highlight;
    color: $foreground;
}
#modal-card Input:focus {
    border-left: solid $accent;
    background: $visual;
}
#storage-editor-preview {
    width: 100%;
    height: auto;
    margin: 1 0 0 0;
    padding: 0 1;
    background: $base;
    color: $fg2;
}
#appearance-preview {
    width: 100%;
    height: auto;
    margin: 0;
    padding: 0 2;
    background: $base;
    border: round $rule;
}
#appearance-preview-banner {
    width: 100%;
    height: auto;
    text-align: center;
}
#appearance-preview-ui {
    width: 100%;
    height: 1;
    margin: 0;
    text-align: center;
    color: $fg2;
}
#appearance-preview-caption {
    width: 100%;
    height: 1;
    text-align: center;
    color: $dim;
}
.modal-card-actions {
    width: 100%;
    height: 2;
    margin: 1 0 0 0;
    align: right middle;
}
.modal-card-actions Button {
    width: auto;
    min-width: 11;
    height: 1;
    min-height: 1;
    margin: 0 0 0 1;
    padding: 0 2;
    border: none;
}
/* Help text is a sentence. Label defaults to width:auto, which sizes to the
   text and then gets clipped by the card instead of wrapping inside it. */
#modal-card .ask-title, #modal-card .ask-hint {
    width: 1fr;
    height: auto;
    padding: 0 1 1 1;
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
    width: 1fr;
    max-width: 100%;
    box-sizing: border-box;
    height: auto;
    min-height: 1;
    max-height: 6;
    overflow-y: auto;
    text-wrap: wrap;
    overflow-x: hidden;
    padding: 0 3 1 3;
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
    width: 82;
    max-width: 94%;
    height: auto;
    max-height: 90%;
    padding: 1 2;
    background: $recessed;
    border: round $rule;
}
.proxy-editor-card Input {
    width: 100%;
    height: 1;
    padding: 0 1;
    border: none;
    border-left: solid $rule;
    background: $highlight;
    color: $foreground;
}
.proxy-editor-card Input:focus {
    border-left: solid $accent;
    background: $visual;
}
.proxy-editor-fields {
    width: 100%;
    height: auto;
    max-height: 20;
    padding: 0 1 1 1;
    background: $base;
    border: round $rule;
    scrollbar-size: 1 1;
}
.proxy-editor-fields VaultCheckbox {
    width: 100%;
    height: 1;
    min-height: 1;
    margin: 1 0 0 0;
    padding: 0 1;
    border: none;
    background: transparent;
}
.proxy-editor-fields VaultCheckbox > .toggle--button {
    width: 4;
    min-width: 4;
    color: $muted;
    background: transparent;
}
.proxy-editor-fields VaultCheckbox.-on > .toggle--button {
    color: $ok;
    background: transparent;
    text-style: bold;
}
.proxy-editor-fields VaultCheckbox > .toggle--label {
    color: $foreground;
}
.proxy-editor-fields VaultCheckbox.-on > .toggle--label {
    color: $foreground;
    text-style: bold;
}
.proxy-editor-fields VaultCheckbox:focus {
    border-left: solid $accent;
    background: $highlight;
}
.proxy-editor-fields VaultCheckbox:focus > .toggle--label {
    color: $foreground;
    background: transparent;
    text-style: bold;
}
#proxy-editor-error {
    width: 100%;
    height: auto;
    max-height: 2;
    padding: 1 0 0 0;
    color: $bad;
}
.proxy-editor-actions {
    width: 100%;
    height: 2;
    margin: 1 0 0 0;
    align: right middle;
}
.proxy-editor-actions Button {
    width: auto;
    min-width: 11;
    height: 1;
    min-height: 1;
    margin: 0 0 0 1;
    padding: 0 2;
    border: none;
}
.proxy-field-label {
    width: 100%;
    height: 1;
    margin: 1 0 0 0;
    color: $muted;
}
#proxy-provider-types {
    height: auto;
    min-height: 3;
    max-height: 10;
    background: $base;
    border: round $rule;
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
    background: $base;
    border: round $rule;
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
    padding: 0 1;
    border: none;
    border-left: solid $rule;
    background: $highlight;
    color: $foreground;
}
#resource-editor-fields Input:focus {
    border-left: solid $accent;
    background: $visual;
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
    border-left: solid $rule;
    background: $highlight;
    color: $foreground;
}
#resource-editor-fields TextArea:focus {
    border-left: solid $accent;
    background: $visual;
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
    height: 94%;
    max-height: 36;
    padding: 1 2;
}
#resource-policy-scroll {
    width: 100%;
    height: 1fr;
    background: transparent;
    scrollbar-size: 1 1;
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
#resource-policy-remote-operations-help {
    width: 100%;
    /* This copy is one line at the policy card width.  Keeping a one-row
       box avoids reserving a second row for bottom padding. */
    height: 1;
    max-height: 1;
    margin: -1 0 0 0;
    padding: 0 1;
    color: $muted;
}
#resource-policy-remote-operations {
    width: 100%;
    height: auto;
    min-height: 4;
    margin: 0 0 1 0;
    padding: 0 1;
    background: $base;
    /* A two-row border consumes both rows and clips all children.  Side
       borders keep the visual grouping without consuming vertical content. */
    border-left: solid $rule;
    border-right: solid $rule;
}
#resource-policy-remote-operations VaultCheckbox {
    width: 100%;
    min-width: 0;
    height: 1;
    min-height: 1;
    padding: 0 1;
    margin: 0;
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
    height: 2;
    min-height: 2;
    margin: 0 0 1 0;
    padding: 0 0 0 1;
    background: $base;
    /* Keep two lines (title + target summary) available inside a compact
       row. A full border on height 2 leaves zero content rows. */
    border-left: solid $rule;
    border-right: solid $rule;
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
    height: 2;
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
    /* Height may shrink, but the masthead's horizontal inset remains 3. */
    padding-left: 3;
    padding-right: 3;
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
.short #resource-editor-card, .short #resource-policy-card, .short #resource-confirm-card,
.short .proxy-editor-card, .short #modal-card, .short .service-policy-card {
    max-height: 94%;
    padding: 0 1;
}
.short .proxy-editor-card .modal-head, .short #modal-card .modal-head, .short .service-policy-card .modal-head {
    margin: 0;
}
.short .proxy-editor-help, .short .modal-card-help {
    padding: 0;
}
.short #modal-card.modal-card-flex,
.short .service-policy-card.modal-card-flex,
.short .proxy-editor-card.modal-card-flex {
    height: 94%;
}
.short #vault-target-card.modal-card-flex {
    height: 92%;
}
.short #vault-target-card.modal-card-flex #vault-target-list {
    height: 1fr;
    max-height: 100%;
}
.short .proxy-editor-actions, .short .modal-card-actions {
    height: 1;
    margin: 0;
}
.short #appearance-preview {
    margin: 0;
    padding: 0 1;
}
.short #appearance-preview-ui {
    margin: 0;
}
.short #appearance-preview-caption {
    display: none;
}
.short #resource-editor-card {
    height: 94%;
}
.short #resource-editor-fields {
    height: 1fr;
}
.short #resource-policy-card {
    height: 94%;
    max-height: 94%;
    padding-top: 0;
    padding-bottom: 0;
}
.short #resource-policy-help, .short .resource-policy-gate-note,
.short #resource-policy-remote-operations-help {
    display: none;
}
.short .resource-policy-section {
    margin: 0;
}
.short #resource-policy-gates {
    height: 3;
    margin: 0;
}
.short #resource-policy-remote-operations {
    height: auto;
    min-height: 4;
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
    border-left: solid $rule;
    border-right: solid $rule;
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
#log-filter {
    width: auto;
    margin: 0 0 0 2;
    padding: 0 1;
    color: $muted;
}
#log-filter:hover {
    background: $highlight;
    color: $foreground;
}
#log-search-toggle {
    width: auto;
    margin: 0 0 0 1;
    padding: 0 1;
    color: $dim;
}
#log-search-toggle:hover {
    background: $highlight;
    color: $foreground;
}
#log-search-bar {
    display: none;
    height: 1;
    margin: 0 0 0 0;
    padding: 0 1;
    background: $recessed;
}
#log-search-input {
    width: 1fr;
    height: 1;
    min-height: 1;
    border: none;
    padding: 0 1;
    margin: 0;
    background: transparent;
    color: $foreground;
}
#log-search-input:focus {
    border: none;
    background: $highlight;
}
#log-search-clear {
    width: auto;
    padding: 0 1;
    color: $muted;
}
#log-search-clear:hover {
    background: $highlight;
    color: $foreground;
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
#import-entry-head {
    height: 2;
    padding: 1 2 0 2;
}
#import-entry-note {
    height: 1;
    padding: 0 2;
}
#import-entry-list {
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
#ready-bar {
    height: 2;
    padding: 1 2 0 2;
}
#ready-head {
    width: 1fr;
    height: 1;
    padding: 0;
}
#ready-copy {
    display: none;
    width: auto;
    height: 1;
    padding: 0 1;
    margin: 0 0 0 2;
    color: $muted;
    background: $recessed;
}
#ready-copy:hover {
    background: $highlight;
    color: $accent;
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

/* --------------------------------------------------------------- short screen
   Everything above is written for a window with room in it: a blank row between
   rows of platforms, a field three rows tall, a footer that does not touch what
   is above it. In a twenty-row split pane that air costs the content itself - the
   platform list was squeezed to nothing - so below `ROOMY_ROWS` it is given back,
   in one place, by a class the app puts on itself when it is resized.

   Air is a luxury; the list is the screen. Same rule as the status line and the
   key bar: lose the least important thing whole. */
/* Keep metadata navigation visible in short terminals. */
.short #attachment-modal {
    height: 86%;
}
.short #attachment-summary, .short #attachment-info {
    height: 1;
    padding: 0 1;
    overflow: hidden;
}
.short #attachment-image {
    min-height: 1;
    padding: 0;
}
.short #attachment-modal-nav, .short #chapter-modal-nav, .short #lyrics-modal-nav {
    margin: 0;
    padding: 0 1;
}
.short #chapter-summary, .short #lyrics-summary {
    padding: 0 1;
}
.short #chapter-modal, .short #lyrics-modal {
    padding: 0 1;
}
.short #banner {
    padding: 0 2;
}
.short #caption {
    height: 1;
    padding: 0 2;
}
.short #body {
    padding: 0 2;
}
.short #setup-notes {
    max-height: 3;
}
.short #setup-note,
.short #service-setup-note,
.short #download-setup-note {
    margin: 0;
}
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
.short #import-entry-head {
    height: 1;
    padding: 0 2;
}
.short .service-grid {
    grid-gutter: 0 1;
}
JumpToBottom {
    color: $muted;
    background: $recessed;
}
JumpToBottom:hover {
    color: $foreground;
    background: $highlight;
}

/* --------------------------------------------------------------- RTL layout
   Active whenever the interface language is right-to-left (Arabic). Mirrors
   text alignment, modal header close marks, action button alignment and side
   accent bars while leaving LTR editable inputs and code/URL fields intact. */
.rtl .modal-head > .modal-close,
.rtl .modal-close,
.rtl #about-modal-close,
.rtl #update-modal-close,
.rtl #shortcuts-modal-close,
.rtl #chapter-modal-close,
.rtl #lyrics-modal-close,
.rtl #attachment-modal-close,
.rtl #job-modal-close,
.rtl #beta-picker-close {
    dock: left;
    width: 5;
    content-align: left middle;
}
.rtl #masthead,
.rtl #subhead,
.rtl #prompt-hint,
.rtl #ident-line,
.rtl #param-line,
.rtl #status-line,
.rtl #delivery-head,
.rtl #delivery-elapsed,
.rtl #delivery-details-body,
.rtl #keybar,
.rtl #resource-manager-help,
.rtl #storage-manager-help,
.rtl #proxy-manager-help,
.rtl #justwatch-settings-help,
.rtl #settings-group-help,
.rtl #storage-preview,
.rtl #storage-editor-preview,
.rtl #proxy-preview,
.rtl .modal-card-title,
.rtl .modal-card-help,
.rtl .proxy-editor-title,
.rtl .proxy-editor-help,
.rtl .proxy-field-label,
.rtl #proxy-editor-error,
.rtl #express-login-status,
.rtl #express-login-code,
.rtl #express-login-url,
.rtl #resource-editor-title,
.rtl #resource-editor-help,
.rtl #resource-policy-title,
.rtl #resource-policy-help,
.rtl #resource-confirm-title,
.rtl #resource-confirm-help,
.rtl .resource-field-label,
.rtl #resource-editor-error,
.rtl .resource-policy-section,
.rtl .resource-policy-gate-note,
.rtl #resource-policy-remote-operations-help,
.rtl .resource-policy-target-title,
.rtl .resource-policy-row Static,
.rtl #resource-editor-fields Select > SelectCurrent Static#label,
.rtl #beta-heading,
.rtl #beta-hint,
.rtl #beta-summary,
.rtl #beta-empty,
.rtl #setup-note,
.rtl #service-setup-note,
.rtl #download-setup-note,
.rtl #vault-target-title,
.rtl #vault-target-help,
.rtl .vault-target-group,
.rtl #vault-target-empty,
.rtl .vault-field-label,
.rtl #vault-keys-help,
.rtl #vault-service-state,
.rtl #vault-destination-state,
.rtl #vault-validation,
.rtl #vault-action-hint,
.rtl #about-modal-title,
.rtl #update-modal-title,
.rtl #shortcuts-modal-title,
.rtl #chapter-modal-title,
.rtl #lyrics-modal-title,
.rtl #attachment-modal-title,
.rtl #job-modal-title,
.rtl #beta-picker-title,
.rtl #about-intro,
.rtl #about-license-note,
.rtl #about-modal-hint,
.rtl #shortcuts-body,
.rtl #shortcuts-modal-hint,
.rtl #update-current,
.rtl #update-status,
.rtl #update-result,
.rtl #update-notes-title,
.rtl #update-remote-notes-title,
.rtl .update-note,
.rtl #update-modal-hint,
.rtl #chapter-summary,
.rtl #chapter-modal-hint,
.rtl .chapter-row,
.rtl #lyrics-summary,
.rtl #lyrics-modal-hint,
.rtl .lyrics-row,
.rtl #job-modal-body,
.rtl #job-modal-hint,
.rtl .ask-title,
.rtl .ask-hint,
.rtl #multi-summary,
.rtl .section-head,
.rtl #import-head,
.rtl #import-note,
.rtl #import-entry-head,
.rtl #import-entry-note,
.rtl #rules-head,
.rtl #rules-note,
.rtl #ready-head,
.rtl .ready-group,
.rtl .ready-row,
.rtl OptionList,
.rtl SelectionList,
.rtl VaultCheckbox,
.rtl Toast {
    text-align: right;
}
.rtl .modal-card-actions,
.rtl .proxy-editor-actions,
.rtl #resource-editor-actions,
.rtl #resource-policy-actions,
.rtl #resource-confirm-actions,
.rtl #vault-target-actions,
.rtl #vault-add-actions {
    align: left middle;
}
.rtl #search-statusline,
.rtl #update-links,
.rtl #resource-manager-actions,
.rtl #proxy-manager-actions {
    align: right middle;
}
.rtl #modal-card Input,
.rtl .proxy-editor-card Input,
.rtl #resource-editor-fields Input,
.rtl #resource-editor-fields TextArea {
    border-left: none;
    border-right: solid $rule;
}
.rtl #modal-card Input:focus,
.rtl .proxy-editor-card Input:focus,
.rtl #resource-editor-fields Input:focus,
.rtl #resource-editor-fields TextArea:focus {
    border-left: none;
    border-right: solid $accent;
}
.rtl #resource-editor-fields {
    padding: 0 2 0 1;
}
.rtl #resource-editor-fields Select > SelectCurrent {
    border-left: none !important;
    border-right: solid $rule !important;
}
.rtl #resource-editor-fields Select > SelectCurrent .arrow {
    dock: left;
    width: 2;
}
.rtl #resource-editor-fields Select:focus > SelectCurrent {
    border-left: none !important;
    border-right: solid $accent !important;
}
.rtl #resource-policy-card VaultCheckbox,
.rtl .vault-target-option,
.rtl #vault-target-all {
    border-left: none;
    border-right: solid transparent;
}
.rtl #resource-editor-fields VaultCheckbox:focus,
.rtl #resource-policy-card VaultCheckbox:focus,
.rtl .proxy-editor-fields VaultCheckbox:focus,
.rtl .vault-target-option:focus,
.rtl #vault-target-all:focus {
    border-left: none;
    border-right: solid $accent;
}
.rtl #resource-policy-remote-operations-help {
    height: auto;
    max-height: 2;
}
.rtl .resource-policy-row {
    padding: 0 1 0 0;
}
.rtl .resource-policy-row Button {
    dock: left;
}
.rtl .resource-policy-row Button:focus {
    border-left: none;
    border-right: solid $accent;
}
.rtl .resource-policy-section {
    border-left: none;
    border-right: solid $accent;
}
.rtl #setup-note,
.rtl #service-setup-note,
.rtl #download-setup-note {
    border-left: none;
    border-right: solid $warn;
}
.rtl BetaHomeScreen #beta-mode {
    border-right: none;
    border-left: solid $rule;
}
.rtl BetaHomeScreen #beta-submit {
    border-left: none;
    border-right: solid $rule;
}
.rtl #delivery-details {
    border-left: none;
    border-right: solid $rule;
}
.rtl #update-result {
    border-left: none;
    border-right: solid $ok;
}
.rtl #recent-bar {
    align-horizontal: right;
}
.rtl .recent-label,
.rtl .recent-chip {
    margin: 0 0 0 1;
}
"""

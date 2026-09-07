"""Block-letter titles.

Two sizes, because the screens are a hierarchy and the title says how deep you
are: six rows for ``unidl`` on the main screen, three for the service name on
the service screen. A terminal has one font at one size, so "smaller" has to be
expressed by drawing smaller letters, not by asking for a smaller font.

Latin names use the hand-drawn half-block glyphs below.  Other scripts are
rasterised into the same three-row ▀▄█ mosaic so names in any script occupy the
same title band.  A framed Unicode tile remains the last resort when no font
can draw the name, and every renderer falls back to plain text when the window
is too narrow to hold the art.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path

from .. import __version__
from .bidi import visual_markup, visual_text

# --------------------------------------------------------------- large: unidl
ART = [
    "██╗   ██╗███╗   ██╗██╗██████╗ ██╗     ",
    "██║   ██║████╗  ██║██║██╔══██╗██║     ",
    "██║   ██║██╔██╗ ██║██║██║  ██║██║     ",
    "██║   ██║██║╚██╗██║██║██║  ██║██║     ",
    "╚██████╔╝██║ ╚████║██║██████╔╝███████╗",
    " ╚═════╝ ╚═╝  ╚═══╝╚═╝╚═════╝ ╚══════╝",
]

ART_WIDTH = max(len(line) for line in ART)

COPYRIGHT = "Copyright © 2026 Chris20"


# ------------------------------------------------------- small: service names
#: Three rows, three columns per glyph, drawn with half blocks. One size down
#: from ART, which is what makes the main screen read as the parent.
GLYPHS: dict[str, tuple[str, str, str]] = {
    "A": ("▄▀▄", "█▀█", "▀ ▀"),
    "B": ("█▀▄", "█▀▄", "▀▀ "),
    "C": ("▄▀▀", "█  ", "▀▀▀"),
    "D": ("█▀▄", "█ █", "▀▀ "),
    "E": ("█▀▀", "█▀ ", "▀▀▀"),
    "F": ("█▀▀", "█▀ ", "▀  "),
    "G": ("▄▀▀", "█ ▄", "▀▀▀"),
    "H": ("█ █", "█▀█", "▀ ▀"),
    "I": ("▀█▀", " █ ", "▀▀▀"),
    "J": ("  █", "  █", "▀▀▀"),
    "K": ("█ █", "█▀▄", "▀ ▀"),
    "L": ("█  ", "█  ", "▀▀▀"),
    "M": ("█▄█", "█▀█", "▀ ▀"),
    "N": ("█▄█", "█ █", "▀ ▀"),
    "O": ("▄▀▄", "█ █", "▀▀▀"),
    "P": ("█▀▄", "█▀ ", "▀  "),
    "Q": ("▄▀▄", "█ █", "▀▀▄"),
    "R": ("█▀▄", "█▀▄", "▀ ▀"),
    "S": ("▄▀▀", " ▀▄", "▀▀ "),
    "T": ("▀█▀", " █ ", " ▀ "),
    "U": ("█ █", "█ █", "▀▀▀"),
    "V": ("█ █", "█ █", " ▀ "),
    "W": ("█ █", "█▀█", "▀ ▀"),
    "X": ("█ █", " █ ", "▀ ▀"),
    "Y": ("█ █", " █ ", " ▀ "),
    "Z": ("▀▀█", " ▄▀", "▀▀▀"),
    "0": ("▄▀▄", "█ █", "▀▀▀"),
    "1": ("▄█ ", " █ ", "▀▀▀"),
    "2": ("▀▀▄", "▄▀ ", "▀▀▀"),
    "3": ("▀▀▄", " ▀▄", "▀▀ "),
    "4": ("█ █", "▀▀█", "  ▀"),
    "5": ("█▀▀", "▀▀▄", "▀▀▀"),
    "6": ("▄▀▀", "█▀▄", "▀▀▀"),
    "7": ("▀▀█", "  █", "  ▀"),
    "8": ("▄▀▄", "█▀█", "▀▀▀"),
    "9": ("▄▀▄", "▀▀█", "▀▀▀"),
    "+": (" ▄ ", "▀█▀", " ▀ "),
    "-": ("   ", "▄▄▄", "   "),
    ".": ("   ", "   ", " ▄ "),
    "/": ("  ▌", " ▌ ", "▌  "),
    "&": ("▄▀▄", "▄▀▄", "▀ ▀"),
    "!": (" █ ", " █ ", " ▀ "),
    "'": (" ▌ ", "   ", "   "),
    " ": ("   ", "   ", "   "),
}

SMALL_ROWS = 3
#: one column between glyphs
_GAP = " "
#: Half-blocks pack two vertical pixels per terminal row. Latin handmade
#: glyphs stay three rows; CJK and other scripts need a 12-pixel bitmap
#: (six rows) or the strokes collapse into a blob.
_MOSAIC_ROWS = 6
_MOSAIC_PIXEL_ROWS = _MOSAIC_ROWS * 2
_HALF_BLOCKS = {
    (False, False): " ",
    (True, False): "▀",
    (False, True): "▄",
    (True, True): "█",
}

#: First existing, loadable face wins.  Order prefers UI sans faces that cover
#: CJK/Hangul/Kana over document serif faces.
_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Hiragino Sans W3.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
)


def art(width: int, colour: str) -> str:
    """The application banner, or a plain title when there is not room."""
    if width < ART_WIDTH + 4:
        return f"[{colour}]unidl[/]"
    return "\n".join(f"[{colour}]{line}[/]" for line in ART)


def caption(version_colour: str, note_colour: str) -> str:
    version, copyright_text = caption_parts(version_colour, note_colour)
    return f"{version}   {copyright_text}"


def caption_parts(version_colour: str, note_colour: str) -> tuple[str, str]:
    """The two independently clickable pieces below the wordmark."""
    return f"[{version_colour}]v{__version__}[/]", f"[{note_colour}]{COPYRIGHT}[/]"


def title_for(name: str) -> str:
    """The part of a service name worth drawing large.

    Service names may carry a qualifier that is useful in a list and noise in
    block letters three rows tall. The full name stays on the identity row just
    below, so nothing is lost.
    """
    head = (name or "").split(" / ")[0].split(" (")[0].strip()
    return head or (name or "").strip()


def _fold_to_glyph(character: str) -> str | None:
    """Map a Latin letter with marks onto the handmade glyph table."""
    upper = character.upper()
    if upper in GLYPHS:
        return upper
    folded = "".join(
        part
        for part in unicodedata.normalize("NFKD", upper)
        if not unicodedata.combining(part)
    )
    return folded if len(folded) == 1 and folded in GLYPHS else None


def small_lines(text: str) -> list[str] | None:
    """``text`` as three rows of block letters.

    Latin names use the hand-drawn three-row glyphs above.  Names with other
    scripts are drawn in the same ▀▄█ mosaic from a system UI font, six rows
    tall so CJK strokes survive.  That still sits below the six-row ``unidl``
    wordmark in weight: it is a bitmap of the real name, not a framed caption.
    """
    source = str(text or "")
    if not source.strip():
        return None
    folded = [_fold_to_glyph(character) for character in source]
    if all(glyph is not None for glyph in folded):
        return _glyph_rows([glyph for glyph in folded if glyph is not None])
    return _mosaic_lines(source) or _unicode_title_lines(source)


def _glyph_rows(characters: list[str]) -> list[str]:
    rows = ["", "", ""]
    for index, character in enumerate(characters):
        glyph = GLYPHS[character]
        for row in range(SMALL_ROWS):
            rows[row] += (_GAP if index else "") + glyph[row]
    return rows


@lru_cache(maxsize=1)
def _font_paths() -> list[str]:
    """Discover banner fonts once per process, not once per service row.

    The home screen can ask for the same CJK-capable face dozens of times while
    it builds or resizes the service grid. Running ``fc-match`` and probing all
    platform font paths for every name made that path needlessly expensive.
    """
    found = [candidate for candidate in _FONT_CANDIDATES if Path(candidate).is_file()]
    extra = _fontconfig_face()
    if extra and extra not in found and Path(extra).is_file():
        found.append(extra)
    return found


def _banner_font_path() -> str | None:
    """Return a loadable UI face, or None when mosaic art cannot be drawn."""
    paths = _font_paths()
    return paths[0] if paths else None


def _fontconfig_face() -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":lang=zh"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or "").strip().split("\n", 1)[0].strip()
    return text or None


@lru_cache(maxsize=8)
def _load_font(pixel_height: int):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    # Draw large, then downsample: a 6-pixel native size cannot hold CJK strokes.
    size = max(24, int(pixel_height) * 8)
    for path in _font_paths():
        for index in range(4):
            try:
                return ImageFont.truetype(path, size=size, index=index)
            except OSError:
                continue
    return None


def _mosaic_lines(text: str) -> list[str] | None:
    """Rasterise ``text`` into three rows of the same half-blocks Latin uses."""
    clean = " ".join(str(text or "").split())
    if not clean:
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    font = _load_font(_MOSAIC_PIXEL_ROWS)
    if font is None:
        return None
    drawn = visual_text(clean)
    try:
        dummy = Image.new("L", (1, 1))
        left, top, right, bottom = ImageDraw.Draw(dummy).textbbox((0, 0), drawn, font=font)
    except (OSError, ValueError):
        return None
    source_width = max(1, right - left)
    source_height = max(1, bottom - top)
    canvas = Image.new("L", (source_width + 2, source_height + 2), 0)
    ImageDraw.Draw(canvas).text((1 - left, 1 - top), drawn, font=font, fill=255)
    target_height = _MOSAIC_PIXEL_ROWS
    target_width = max(
        target_height,
        int(round(source_width * target_height / source_height)),
    )
    bitmap = canvas.resize((target_width, target_height), Image.Resampling.LANCZOS)
    pixels = bitmap.load()
    samples = [
        bitmap.getpixel((x, y))
        for y in range(target_height)
        for x in range(target_width)
    ]
    if not any(value > 0 for value in samples):
        return None
    # Keep thin CJK strokes: anything above a low cut counts as ink.
    cut = max(24, sorted(samples)[len(samples) // 3] // 2)
    rows: list[str] = []
    for origin in range(0, target_height, 2):
        cells: list[str] = []
        for x in range(target_width):
            upper = pixels[x, origin] > cut
            lower = pixels[x, origin + 1] > cut if origin + 1 < target_height else False
            cells.append(_HALF_BLOCKS[(upper, lower)])
        rows.append("".join(cells).rstrip() or " ")
    if len(rows) != _MOSAIC_ROWS or not any(row.strip() for row in rows):
        return None
    width = max(len(row) for row in rows)
    return [row.ljust(width) for row in rows]


def _cell_width(text: str) -> int:
    """Return terminal cells for text, including wide East Asian characters."""
    width = 0
    for character in text:
        if unicodedata.combining(character) or unicodedata.category(character) in {
            "Mn",
            "Me",
            "Cf",
        }:
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def _unicode_title_lines(text: str) -> list[str]:
    """Draw an arbitrary-script title in the same three-row space."""
    # Keep this middle row in logical Unicode order.  ``small_art`` applies the
    # bidi/Arabic shaping pass exactly once while escaping it for Rich markup.
    # Reordering here as well would make RTL titles change direction twice (and
    # would turn shaped Arabic back into a logical-looking sequence).
    clean = " ".join(str(text or "").split())
    content_width = max(1, _cell_width(clean))
    padding = max(0, content_width - _cell_width(clean))
    middle = f"│ {clean}{' ' * padding} │"
    edge = "─" * (content_width + 2)
    return [f"╭{edge}╮", middle, f"╰{edge}╯"]


def small_art(text: str, width: int, colour: str) -> str:
    """Service name one size down from :func:`art`, with a plain fallback."""
    lines = small_lines(text)
    if lines is None or max(_cell_width(line) for line in lines) > max(0, width - 4):
        return f"[{colour}]{visual_markup(text)}[/]"
    return "\n".join(f"[{colour}]{visual_markup(line)}[/]" for line in lines)


def small_height(text: str, width: int) -> int:
    """Rows :func:`small_art` will occupy, so the layout can reserve them."""
    lines = small_lines(text)
    if lines is None or max(_cell_width(line) for line in lines) > max(0, width - 4):
        return 1
    return len(lines)


__all__ = [
    "ART",
    "ART_WIDTH",
    "COPYRIGHT",
    "GLYPHS",
    "SMALL_ROWS",
    "_MOSAIC_ROWS",
    "art",
    "caption",
    "small_art",
    "small_height",
    "small_lines",
    "title_for",
]

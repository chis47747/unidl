"""Subtitle placement shared by text and fragmented-MP4 subtitle readers.

Coordinates are percentages of the display, not pixels from a provider's canvas.
WebVTT is kept verbatim by its writer; ASS maps the common horizontal layout.
Vertical writing and arbitrary WebVTT CSS are not representable by this model.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubtitleLayout:
    alignment: int = 2  # ASS numpad alignment (2 = bottom centre).
    x: float | None = None
    y: float | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str | None = None


def vtt_blocks(text: str) -> tuple[str, ...]:
    return tuple(
        block.strip()
        for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
        if re.match(r"^(?:STYLE|REGION)\b", block.strip())
    )


def vtt_settings(value: str) -> dict[str, str]:
    return dict(re.findall(r"\b(line|position|size|align|vertical|region):([^\s]+)", value))


def percentage(value: str | None) -> float | None:
    if value is None or not value.endswith("%"):
        return None
    try:
        return max(0.0, min(100.0, float(value[:-1])))
    except ValueError:
        return None


def vtt_layout(settings: str, blocks: tuple[str, ...] = ()) -> SubtitleLayout:
    values = vtt_settings(settings)
    horizontal = {"start": 1, "left": 1, "end": 3, "right": 3}.get(values.get("align", ""), 2)
    position = values.get("position", "").split(",")
    x = percentage(position[0])
    if len(position) > 1:
        horizontal = {"line-left": 1, "center": 2, "line-right": 3}.get(position[1], horizontal)
    line = values.get("line", "").split(",")
    y = percentage(line[0])
    vertical = 1  # Bottom when no explicit line was supplied.
    if y is not None:
        vertical = {"start": 3, "center": 2, "end": 1}.get(line[-1], 3)
    elif re.fullmatch(r"-?\d+", line[0]):
        number = int(line[0])
        # Snap-to-lines depends on player/font size; approximate on an ASS canvas.
        vertical = 3 if number >= 0 else 1
        y = min(95.0, 5.0 + number * 5.0) if number >= 0 else max(5.0, 100.0 + number * 5.0)
    if "region" in values and "line" not in values:
        for block in blocks:
            if not block.startswith("REGION"):
                continue
            region = dict(re.findall(r"([\w]+):([^\n]+)", block))
            if region.get("id", "").strip() != values["region"]:
                continue
            anchor = region.get("regionanchor", "0%,100%").split(",")
            viewport = region.get("viewportanchor", "0%,100%").split(",")
            if len(anchor) == len(viewport) == 2:
                rx, ry = (percentage(part.strip()) for part in anchor)
                vx, vy = (percentage(part.strip()) for part in viewport)
                width = percentage(region.get("width", "100%").strip()) or 100.0
                if rx is not None and vx is not None:
                    x = vx - width * rx / 100.0 + width * (horizontal - 1) / 2.0
                if ry is not None and vy is not None:
                    y = vy
                    vertical = 3 if ry < 33 else 1 if ry > 66 else 2
            break
    return SubtitleLayout(alignment=(vertical - 1) * 3 + horizontal, x=x, y=y)


def css_styles(blocks: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Read common cue-wide emphasis/colour; retain all other CSS in VTT."""
    result: dict[str, dict[str, str]] = {}
    for block in blocks:
        if not block.startswith("STYLE"):
            continue
        for selector, body in re.findall(r"::cue(?:\(([^)]*)\))?\s*\{([^}]*)\}", block):
            selector = selector.strip()
            if selector and not re.fullmatch(r"\.[\w-]+", selector):
                continue
            result.setdefault(selector.lstrip("."), {}).update(
                (key.lower(), value.strip())
                for key, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", body)
            )
    return result


def ass_color(value: str | None) -> str | None:
    colors = {"white": "ffffff", "black": "000000", "red": "ff0000", "yellow": "ffff00",
              "lime": "00ff00", "green": "008000", "blue": "0000ff", "cyan": "00ffff", "magenta": "ff00ff"}
    color = colors.get((value or "").lower(), (value or "").lstrip("#"))
    if re.fullmatch(r"[a-fA-F0-9]{3}", color):
        color = "".join(char * 2 for char in color)
    if not re.fullmatch(r"[a-fA-F0-9]{6}(?:[a-fA-F0-9]{2})?", color):
        return None
    return f"&H{color[4:6]}{color[2:4]}{color[:2]}&".upper()


def ass_text(payload: str, layout: SubtitleLayout, blocks: tuple[str, ...]) -> str:
    styles = css_styles(blocks)
    base = {"bold": layout.bold, "italic": layout.italic, "underline": layout.underline, "color": layout.color}

    def apply(style, declarations):
        style = dict(style)
        if "font-weight" in declarations:
            style["bold"] = declarations["font-weight"] in {"bold", "700", "800", "900"}
        if "font-style" in declarations:
            style["italic"] = declarations["font-style"] in {"italic", "oblique"}
        if "text-decoration" in declarations:
            style["underline"] = "underline" in declarations["text-decoration"]
        if "color" in declarations:
            style["color"] = declarations["color"]
        return style

    def tags(style):
        color = ass_color(style["color"]) or "&HFFFFFF&"
        return "{" + f"\\b{int(style['bold'])}\\i{int(style['italic'])}\\u{int(style['underline'])}\\c{color}" + "}"

    base = apply(base, styles.get("", {}))
    stack = [("", base)]
    result = [tags(base)] if any(base.values()) else []
    for token in re.split(r"(<[^>]+>)", payload):
        match = re.fullmatch(r"<(/?)(b|i|u|c)([^>]*)>", token, flags=re.IGNORECASE)
        if match:
            closing, name, extra = match.groups()
            name = name.lower()
            if closing:
                for index in range(len(stack) - 1, 0, -1):
                    if stack[index][0] == name:
                        del stack[index:]
                        break
            else:
                style = dict(stack[-1][1])
                if name == "c":
                    for class_name in extra.lstrip(".").split("."):
                        style = apply(style, styles.get(class_name, {}))
                else:
                    style[{"b": "bold", "i": "italic", "u": "underline"}[name]] = True
                stack.append((name, style))
            result.append(tags(stack[-1][1]))
        elif token.startswith("<"):
            if re.fullmatch(r"<br\s*/?>", token, flags=re.IGNORECASE):
                result.append(r"\N")
        else:
            result.append(html.unescape(token).replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N"))
    return "".join(result)


def srt_text(payload: str) -> str:
    # Keep widely supported emphasis, but not WebVTT voice/class/timestamp tags.
    return html.unescape(re.sub(r"<[^>]+>", lambda match: match[0] if re.fullmatch(r"</?[biu]>", match[0]) else "", payload))

"""Safe, title-aware output directory templates."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from . import template
from .titles import Title, TitleKind

EPISODE_FOLDER_FIELDS = frozenset(
    {"series", "season", "season_number", "year", "season_episode", "service"}
)
DEFAULT_EPISODE_FOLDER_TEMPLATE = ""
EPISODE_FOLDER_PRESETS = {
    "": "Flat",
    "{series}": "Series",
    "{series}/{season}": "Series / Season",
}

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_INVALID_COMPONENT = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_RESERVED_COMPONENT = re.compile(
    r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$"
)


def safe_folder_component(value: object, *, fallback: str = "Unknown") -> str:
    """Make one title-derived path component safe on POSIX and Windows."""

    text = str(value or "").strip()
    text = text.replace("/", "_")
    text = _INVALID_COMPONENT.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text or text in {".", ".."}:
        text = fallback
    if _RESERVED_COMPONENT.fullmatch(text):
        text = f"_{text}"
    return text[:120].rstrip(" .") or fallback


def episode_folder_fields(title: Title, service: str = "") -> dict[str, str]:
    """Return stable, human-readable variables for an episode directory."""

    season = title.season
    season_number = f"{int(season):02d}" if season is not None else ""
    season_label = f"Season {season_number}" if season_number else "Specials"
    episode = int(title.episode or 0)
    season_episode = (
        f"S{int(season):02d}E{episode:02d}"
        if season is not None and episode
        else (f"E{episode:02d}" if episode else "")
    )
    return {
        "series": safe_folder_component(title.name, fallback="Unknown Series"),
        "season": safe_folder_component(season_label, fallback="Specials"),
        "season_number": season_number,
        # A season can cross a calendar year. Prefer the containing series'
        # premiere/release year so ``{series} ({year})`` remains one root
        # folder; older/service titles fall back to the episode year.
        "year": safe_folder_component(
            title.series_year or title.year, fallback=""
        ) if (title.series_year or title.year) else "",
        "season_episode": season_episode,
        "service": safe_folder_component(service, fallback="unknown"),
    }


def validate_episode_folder_template(value: str | None) -> list[str]:
    """Validate a relative episode folder template for the storage manager."""

    text = str(value or "").strip()
    if not text:
        return []
    problems: list[str] = []
    if "\x00" in text or "\n" in text or "\r" in text:
        problems.append("the folder template must be one line")
    if text.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(text):
        problems.append("the folder template must be relative to the service folder")
    if any(part in {".", ".."} for part in re.split(r"[/\\]", text)):
        problems.append(". and .. path components are not allowed")
    literal = re.sub(r"\{[a-z_][a-z0-9_]*\??\}", "", text)
    bad = sorted(set(_INVALID_COMPONENT.findall(literal)))
    if bad:
        problems.append(f"illegal folder characters: {' '.join(bad)}")
    unknown = [
        name
        for name in template.variables(text)
        if name not in EPISODE_FOLDER_FIELDS
    ]
    if unknown:
        problems.append(
            f"unknown folder field(s): {', '.join('{' + name + '}' for name in unknown)}; "
            f"available: {', '.join(sorted(EPISODE_FOLDER_FIELDS))}"
        )
    if "{" in text or "}" in text:
        rendered_fields = set(template.variables(text))
        unmatched = [char for char in ("{", "}") if char in text and not rendered_fields]
        if unmatched:
            problems.append("folder fields must use {name} or {name?} syntax")
    return problems


def render_episode_folder(
    folder_template: str | None,
    title: Title,
    service: str = "",
) -> tuple[str, ...]:
    """Render a validated template as safe relative directory components."""

    text = str(folder_template or "").strip()
    if title.kind != TitleKind.EPISODE or not text:
        return ()
    problems = validate_episode_folder_template(text)
    if problems:
        raise ValueError("; ".join(problems))
    rendered = template.render(text, episode_folder_fields(title, service))
    components: list[str] = []
    for raw in PurePosixPath(rendered.replace("\\", "/")).parts:
        if raw in {"", "."}:
            continue
        if raw == "..":
            raise ValueError("rendered folder template escaped the service directory")
        components.append(safe_folder_component(raw))
    return tuple(components)


__all__ = [
    "DEFAULT_EPISODE_FOLDER_TEMPLATE",
    "EPISODE_FOLDER_FIELDS",
    "EPISODE_FOLDER_PRESETS",
    "episode_folder_fields",
    "render_episode_folder",
    "safe_folder_component",
    "validate_episode_folder_template",
]

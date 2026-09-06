"""Package-local interface language.

This is not process ``gettext`` and it does not call ``setlocale``. UniDL can
host several sessions in one process; a service client and the user's shell keep
whatever locale they already had. Message IDs are stable (``chrome.back``);
English sentences in existing widgets are mapped to those IDs so the first
rollout does not have to rewrite every call site.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

SUPPORTED_LOCALES = ("en", "zh-Hans", "zh-Hant", "es", "fr", "pt")
DEFAULT_LOCALE = "en"

#: Known English chrome / key-bar / status phrases → stable message IDs.
#: Widgets may still pass the English they already had; catalogues never use the
#: English sentence as a key.
_PHRASES: dict[str, str] = {
    "Back": "chrome.back",
    "Quit": "chrome.quit",
    "Search": "chrome.search",
    "Settings": "chrome.settings",
    "Pause": "chrome.pause",
    "Resume": "chrome.resume",
    "Stopping": "chrome.stopping",
    "Starting": "chrome.starting",
    "Done": "chrome.done",
    "Chapters": "chrome.chapters",
    "back": "keys.back",
    "confirm": "keys.confirm",
    "move": "keys.move",
    "log": "keys.log",
    "settings": "keys.settings",
    "search": "keys.search",
    "cancel": "keys.cancel",
    "change": "keys.change",
    "reset": "keys.reset",
    "quit": "keys.quit",
    "open": "keys.open",
    "scroll": "keys.scroll",
    "chapters": "keys.chapters",
    "stopping...": "keys.stopping",
    "stop everything": "keys.stop_everything",
    "expand the log": "keys.log_expand",
    "collapse the log": "keys.log_collapse",
    "show the log": "keys.log_show",
    "toggle the log": "keys.log_toggle",
    "select text to copy it": "keys.drag_copy",
    "open the highlighted platform": "keys.open_highlighted",
    "service view": "keys.service_view",
    "after picking": "keys.after_picking",
    "dev reload": "keys.dev_reload",
    "commands": "keys.commands",
    "back one step": "keys.back_one_step",
    "back to the menu": "keys.back_to_menu",
    "back to platforms": "keys.back_to_platforms",
    "jump to a number": "keys.jump_number",
    "copy one": "keys.copy_one",
    "copy all": "keys.copy_all",
    "copy the command": "keys.copy_command",
    "open manager": "keys.open_manager",
    "use this value": "keys.use_this",
    "toggle": "keys.toggle",
    "apply": "keys.apply",
    "skip just this one": "keys.skip_one",
    "run it again": "keys.retry_one",
    "settings for this service": "keys.settings_service",
    "search this service's keys": "keys.search_service_keys",
    "download the file": "mode.download",
    "save the command only": "mode.command",
    "list the tracks only": "mode.list",
    "save an export file": "mode.export",
    "ask me": "mode.ask",
    "on": "value.on",
    "off": "value.off",
    "save": "keys.save",
    "edit": "keys.edit",
    "add": "keys.add",
    "delete": "keys.delete",
    "choose": "keys.choose",
    "reload": "keys.reload",
    "authorize": "keys.authorize",
    "test": "keys.test",
    "add keys": "keys.add_keys",
    "copy key": "keys.copy_key",
    "review and add": "keys.review_add",
    "move between fields": "keys.move_fields",
    "use this device": "keys.use_device",
    "local / remote": "keys.local_remote",
    "manage CDMs": "keys.manage_cdms",
    "open the cdm folder": "keys.open_cdm_folder",
    "add a rule": "keys.add_rule",
    "remove this rule": "keys.remove_rule",
    "back, saving": "keys.back_saving",
    "change the device": "keys.change_device",
    "import this one": "keys.import_one",
    "look again": "keys.look_again",
    "open the folder": "keys.open_folder",
    "check again": "keys.check_again",
    "copy the report": "keys.copy_report",
    "open browser": "keys.open_browser",
    "copy code": "keys.copy_code",
    "use / edit": "keys.use_edit",
    "add remote": "keys.add_remote",
    "import local DBs": "keys.import_local",
    "enable / disable": "keys.enable_disable",
    "vault policy": "keys.vault_policy",
    "tick": "keys.tick",
    "discard": "keys.discard",
    "defaults": "keys.defaults",
    "use this region": "keys.use_region",
    "default": "keys.default",
    "open in a service": "keys.open_service",
    "copy link": "keys.copy_link",
    "regions": "keys.regions",
    "Enabled": "value.enabled",
    "Cancel": "common.cancel",
    "Delete": "common.delete",
    "Apply": "common.apply",
    "Save": "common.save",
    "Select": "common.select",
    "Yes": "common.yes",
    "No": "common.no",
}

#: English status tokens the native downloader paints into the TUI frame.
#: Longer tokens first so "Decrypting" is not split by "Decrypt".
_PROGRESS_TOKENS: tuple[tuple[str, str], ...] = (
    ("Downloading:", "progress.downloading_colon"),
    ("Queued:", "progress.queued_colon"),
    ("Downloading", "progress.downloading"),
    ("Downloaded", "progress.downloaded"),
    ("Decrypting", "progress.decrypting"),
    ("Decrypted", "progress.decrypted"),
    ("Repacking", "progress.repacking"),
    ("Repacked", "progress.repacked"),
    ("Recording", "progress.recording"),
    ("Recorded", "progress.recorded"),
    ("Waiting", "progress.waiting"),
    ("Queued", "progress.queued"),
    ("Paused", "progress.paused"),
    ("Stopped", "progress.stopped"),
    ("Muxing", "progress.muxing"),
    ("Cached", "progress.cached"),
    ("Failed", "progress.failed"),
    ("Error", "progress.error"),
    ("Done", "progress.done"),
)

_LOCALE_ALIASES = {
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "en_us": "en",
    "en_gb": "en",
    "c": "en",
    "posix": "en",
    "es": "es",
    "es-es": "es",
    "es_es": "es",
    "es-419": "es",
    "es_419": "es",
    "fr": "fr",
    "fr-fr": "fr",
    "fr_fr": "fr",
    "fr-ca": "fr",
    "fr_ca": "fr",
    "fr-be": "fr",
    "fr_be": "fr",
    "fr-ch": "fr",
    "fr_ch": "fr",
    "fr-lu": "fr",
    "fr_lu": "fr",
    "pt": "pt",
    "pt-pt": "pt",
    "pt_pt": "pt",
    "pt-br": "pt",
    "pt_br": "pt",
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh_cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh_sg": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh_hans": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh_tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh_hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "zh_mo": "zh-Hant",
    "zh-hant": "zh-Hant",
    "zh_hant": "zh-Hant",
}

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_current = None


class _SafeMap(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def supported_locales() -> tuple[str, ...]:
    return SUPPORTED_LOCALES


def detect_locale() -> str:
    """Best interface language from the process environment, else English."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(name)
        if raw:
            resolved = resolve_locale(raw)
            if resolved != DEFAULT_LOCALE or _looks_english(raw):
                return resolved
    return DEFAULT_LOCALE


def _looks_english(raw: str) -> bool:
    token = raw.strip().split(".", 1)[0].split("@", 1)[0].replace("-", "_").lower()
    return token in {"en", "en_us", "en_gb", "c", "posix", ""}


def resolve_locale(value: object) -> str:
    """Map a setting or env token onto a supported catalogue ID."""
    text = str(value or "").strip()
    if not text or text.casefold() in {"system", "auto", "default"}:
        return detect_locale()
    token = text.split(".", 1)[0].split("@", 1)[0]
    folded = token.replace("_", "-").casefold()
    if folded in _LOCALE_ALIASES:
        return _LOCALE_ALIASES[folded]
    underscored = token.replace("-", "_").casefold()
    if underscored in _LOCALE_ALIASES:
        return _LOCALE_ALIASES[underscored]
    language = folded.split("-", 1)[0]
    if language in _LOCALE_ALIASES:
        return _LOCALE_ALIASES[language]
    return DEFAULT_LOCALE


def cell_width(text: str) -> int:
    """Terminal columns for ``text``, counting East-Asian wide characters as two."""
    width = 0
    for char in str(text or ""):
        if char in "\n\t":
            width += 1
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _catalogue_text(locale: str) -> str:
    name = f"{locale}.json"
    try:
        return resources.files("unidl").joinpath("locales", name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError, ValueError):
        path = Path(__file__).resolve().parent.parent / "locales" / name
        return path.read_text(encoding="utf-8")


def load_catalogue(locale: str) -> dict[str, str]:
    """Load one UTF-8 JSON catalogue. Missing or broken files are empty."""
    try:
        document = json.loads(_catalogue_text(locale))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in document.items():
        if isinstance(key, str) and isinstance(value, str):
            result[key] = value
    return result


class Translator:
    """Look up ``message_id`` in the active catalogue, then English, then a default."""

    def __init__(self, locale: str = DEFAULT_LOCALE, *, debug: bool = False) -> None:
        resolved = locale if locale in SUPPORTED_LOCALES else resolve_locale(locale)
        self.locale = resolved if resolved in SUPPORTED_LOCALES else DEFAULT_LOCALE
        self.debug = bool(debug)
        self.english = load_catalogue(DEFAULT_LOCALE)
        self.messages = (
            dict(self.english)
            if self.locale == DEFAULT_LOCALE
            else {**self.english, **load_catalogue(self.locale)}
        )
        self.missing: set[str] = set()

    def tr(self, message_id: str, default: str | None = None, **values: Any) -> str:
        ident = str(message_id or "").strip()
        if not ident:
            return str(default or "")
        template = self._template(ident, default, values)
        if not values:
            return template
        try:
            return template.format_map(_SafeMap({key: values[key] for key in values}))
        except (IndexError, ValueError):
            return template

    def _template(self, ident: str, default: str | None, values: Mapping[str, Any]) -> str:
        count = values.get("count")
        if isinstance(count, int):
            plural = f"{ident}.one" if count == 1 else f"{ident}.other"
            if plural in self.messages:
                return self.messages[plural]
        if ident in self.messages:
            return self.messages[ident]
        if default is not None:
            if self.debug:
                self.missing.add(ident)
            return default
        if self.debug:
            self.missing.add(ident)
        return ident

    def phrase(self, text: str, default: str | None = None, **values: Any) -> str:
        """Translate a chrome/key-bar phrase, accepting English or a message ID."""
        ident = _PHRASES.get(text, text)
        fallback = default if default is not None else text
        return self.tr(ident, default=fallback, **values)


def set_translator(translator: Translator) -> None:
    global _current
    _current = translator


def get_translator() -> Translator:
    global _current
    if _current is None:
        _current = Translator(detect_locale())
    return _current


def tr(message_id: str, default: str | None = None, **values: Any) -> str:
    return get_translator().tr(message_id, default=default, **values)


def phrase(text: str, default: str | None = None, **values: Any) -> str:
    return get_translator().phrase(text, default=default, **values)


def setting_label(spec: Any) -> str:
    key = getattr(spec, "key", "")
    fallback = str(getattr(spec, "label", "") or key)
    return tr(f"setting.{key}", default=fallback)


def setting_help(spec: Any) -> str:
    key = getattr(spec, "key", "")
    fallback = str(getattr(spec, "help", "") or "")
    if not fallback:
        return ""
    return tr(f"setting.{key}.help", default=fallback)


def option_label(spec: Any, option: Any) -> str:
    key = getattr(spec, "key", "")
    value = getattr(option, "value", option)
    fallback = option.display() if hasattr(option, "display") else str(getattr(option, "label", None) or value)
    token = str(value) if str(value) else "app_wide"
    return tr(f"setting.{key}.option.{token}", default=fallback)


def localize_progress_line(text: str) -> str:
    """Translate only the status portion of a native-downloader progress row.

    Track labels and output paths are user/provider data and may legitimately
    contain words such as ``Done`` or ``Downloading``. Structured progress rows
    put their current state after the final `` | ``; standalone transient rows
    begin with the state token. Restricting replacement to those portions keeps
    filenames and titles lossless while still localizing mux/decrypt statuses.
    """
    result = str(text or "")
    if not result:
        return result
    prefix, separator, segment = result.rpartition(" | ")
    if separator:
        head = prefix + separator
    else:
        head, segment = "", result
        stripped = segment.lstrip()
        starts_status = any(
            stripped.startswith(english)
            and (
                len(stripped) == len(english)
                or stripped[len(english)] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
            )
            for english, _ident in _PROGRESS_TOKENS
        )
        # Structured VOD rows have no pipe before their final state, but they
        # do have a progress bar/percentage. Only a token at the end (optionally
        # followed by the native check/spinner glyph) is a status in that shape.
        progress_row = "━" in segment or "─" in segment or "%" in segment
        if not starts_status and not progress_row:
            return result
    for english, ident in _PROGRESS_TOKENS:
        translated = tr(ident, default=english)
        if translated == english:
            continue
        if separator or segment.lstrip().startswith(english):
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(english)}(?=$|[^A-Za-z0-9_])"
        else:
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(english)}(?=\s*(?:✓|[⣾⣽⣻⢿⡿⣟⣯⢿]|$))"
        segment = re.sub(pattern, translated, segment)
    return head + segment


def setting_value(spec: Any, scope: Any) -> str:
    """Translated current value for a settings row."""
    if getattr(spec, "kind", "") == "bool":
        return tr("value.on") if scope.get(spec.key) else tr("value.off")
    if getattr(spec, "picker", "") in {
        "resources",
        "storage",
        "proxy_manager",
        "justwatch",
        "download_behavior",
        "interface",
    }:
        return phrase("open manager")
    value = scope.get(spec.key)
    for option in getattr(spec, "options", ()) or ():
        if option.value == value:
            return option_label(spec, option)
    return str(scope.label_for(spec.key))


def unknown_placeholders(catalogue: Mapping[str, str], english: Mapping[str, str]) -> dict[str, set[str]]:
    """IDs whose translation invents placeholder names the English source lacks."""
    problems: dict[str, set[str]] = {}
    for ident, text in catalogue.items():
        source = english.get(ident)
        if source is None:
            continue
        extra = set(_PLACEHOLDER.findall(text)) - set(_PLACEHOLDER.findall(source))
        if extra:
            problems[ident] = extra
    return problems


__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "Translator",
    "cell_width",
    "detect_locale",
    "get_translator",
    "load_catalogue",
    "option_label",
    "phrase",
    "resolve_locale",
    "set_translator",
    "setting_help",
    "setting_label",
    "setting_value",
    "supported_locales",
    "tr",
    "localize_progress_line",
    "unknown_placeholders",
]

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
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from .terminal_cells import cell_width

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


_DYNAMIC_SETTING_PHRASES: dict[str, tuple[tuple[str, str], ...]] = {
    "zh-Hans": (
        ("Auto / best available", "自动 / 最高可用"),
        ("Best available", "最高可用"),
        ("Manifest profile", "清单配置"),
        ("Manifest resolution", "清单分辨率"),
        ("Manifest color range", "清单色彩范围"),
        ("Manifest codec", "清单编码"),
        ("Playback source", "播放源"),
        ("Playback scenario", "播放场景"),
        ("Authorization method", "授权方式"),
        ("Sign-in method", "登录方式"),
        ("Login method", "登录方式"),
        ("Browser cookies", "浏览器 Cookie"),
        ("Apple ID account", "Apple ID 账户"),
        ("full ladder", "完整画质阶梯"),
        ("Dynamic range", "动态范围"),
        ("Source quality", "源质量"),
        ("Stream quality", "流媒体质量"),
        ("Video quality", "视频质量"),
        ("Audio quality", "音频质量"),
        ("Video profile", "视频配置"),
        ("Audio profile", "音频配置"),
        ("Metadata language", "元数据语言"),
        ("Catalogue language", "目录语言"),
        ("Catalog languages", "目录语言"),
        ("best available", "最高可用"),
        ("Automatic", "自动"),
        ("Auto", "自动"),
        ("Playback", "播放"),
        ("Manifest", "清单"),
        ("Source", "源"),
        ("Stream", "流"),
        ("Profile", "配置"),
        ("Quality", "质量"),
        ("Resolution", "分辨率"),
        ("Codec", "编码"),
        ("Language", "语言"),
        ("Region", "地区"),
        ("Country", "国家"),
        ("Account", "账户"),
        ("Device", "设备"),
        ("device", "设备"),
        ("Live", "直播"),
        ("Market", "市场"),
        ("Delivery", "传输"),
        ("Requested", "请求的"),
        ("Prefer", "优先"),
        ("Ask", "询问"),
        ("Any", "任意"),
        ("Main", "主要"),
        ("Primary", "主要"),
    ),
    "zh-Hant": (
        ("Auto / best available", "自動 / 最高可用"),
        ("Best available", "最高可用"),
        ("Manifest profile", "清單設定"),
        ("Manifest resolution", "清單解析度"),
        ("Manifest color range", "清單色彩範圍"),
        ("Manifest codec", "清單編碼"),
        ("Playback source", "播放來源"),
        ("Playback scenario", "播放情境"),
        ("Authorization method", "授權方式"),
        ("Sign-in method", "登入方式"),
        ("Login method", "登入方式"),
        ("Browser cookies", "瀏覽器 Cookie"),
        ("Apple ID account", "Apple ID 帳戶"),
        ("full ladder", "完整畫質階梯"),
        ("Dynamic range", "動態範圍"),
        ("Source quality", "來源品質"),
        ("Stream quality", "串流品質"),
        ("Video quality", "視訊品質"),
        ("Audio quality", "音訊品質"),
        ("Video profile", "視訊設定"),
        ("Audio profile", "音訊設定"),
        ("Metadata language", "中繼資料語言"),
        ("Catalogue language", "目錄語言"),
        ("Catalog languages", "目錄語言"),
        ("best available", "最高可用"),
        ("Automatic", "自動"),
        ("Auto", "自動"),
        ("Playback", "播放"),
        ("Manifest", "清單"),
        ("Source", "來源"),
        ("Stream", "串流"),
        ("Profile", "設定"),
        ("Quality", "品質"),
        ("Resolution", "解析度"),
        ("Codec", "編碼"),
        ("Language", "語言"),
        ("Region", "地區"),
        ("Country", "國家"),
        ("Account", "帳戶"),
        ("Device", "裝置"),
        ("device", "裝置"),
        ("Live", "直播"),
        ("Market", "市場"),
        ("Delivery", "傳輸"),
        ("Requested", "要求的"),
        ("Prefer", "優先"),
        ("Ask", "詢問"),
        ("Any", "任意"),
        ("Main", "主要"),
        ("Primary", "主要"),
    ),
    "es": (
        ("Auto / best available", "Automático / mejor disponible"),
        ("Best available", "Mejor disponible"),
        ("Manifest profile", "Perfil del manifiesto"),
        ("Manifest resolution", "Resolución del manifiesto"),
        ("Manifest color range", "Rango de color del manifiesto"),
        ("Manifest codec", "Códec del manifiesto"),
        ("Playback source", "Fuente de reproducción"),
        ("Playback scenario", "Escenario de reproducción"),
        ("Authorization method", "Método de autorización"),
        ("Sign-in method", "Método de inicio de sesión"),
        ("Login method", "Método de inicio de sesión"),
        ("Browser cookies", "Cookies del navegador"),
        ("Apple ID account", "Cuenta de Apple ID"),
        ("full ladder", "escalera completa"),
        ("Dynamic range", "Rango dinámico"),
        ("Source quality", "Calidad de origen"),
        ("Stream quality", "Calidad de transmisión"),
        ("Video quality", "Calidad de vídeo"),
        ("Audio quality", "Calidad de audio"),
        ("Video profile", "Perfil de vídeo"),
        ("Audio profile", "Perfil de audio"),
        ("Metadata language", "Idioma de metadatos"),
        ("Catalogue language", "Idioma del catálogo"),
        ("Catalog languages", "Idiomas del catálogo"),
        ("best available", "mejor disponible"),
        ("Automatic", "Automático"),
        ("Auto", "Automático"),
        ("Playback", "Reproducción"),
        ("Manifest", "Manifiesto"),
        ("Source", "Origen"),
        ("Stream", "Transmisión"),
        ("Profile", "Perfil"),
        ("Quality", "Calidad"),
        ("Resolution", "Resolución"),
        ("Codec", "Códec"),
        ("Language", "Idioma"),
        ("Region", "Región"),
        ("Country", "País"),
        ("Account", "Cuenta"),
        ("Device", "Dispositivo"),
        ("device", "dispositivo"),
        ("Live", "Directo"),
        ("Market", "Mercado"),
        ("Delivery", "Entrega"),
        ("Requested", "Solicitado"),
        ("Prefer", "Preferir"),
        ("Ask", "Preguntar"),
        ("Any", "Cualquiera"),
        ("Main", "Principal"),
        ("Primary", "Principal"),
    ),
    "fr": (
        ("Auto / best available", "Auto / meilleure qualité disponible"),
        ("Best available", "Meilleure qualité disponible"),
        ("Manifest profile", "Profil du manifeste"),
        ("Manifest resolution", "Résolution du manifeste"),
        ("Manifest color range", "Gamme de couleurs du manifeste"),
        ("Manifest codec", "Codec du manifeste"),
        ("Playback source", "Source de lecture"),
        ("Playback scenario", "Scénario de lecture"),
        ("Authorization method", "Méthode d'autorisation"),
        ("Sign-in method", "Méthode de connexion"),
        ("Login method", "Méthode de connexion"),
        ("Browser cookies", "Cookies du navigateur"),
        ("Apple ID account", "Compte Apple ID"),
        ("full ladder", "échelle complète"),
        ("Dynamic range", "Plage dynamique"),
        ("Source quality", "Qualité de la source"),
        ("Stream quality", "Qualité du flux"),
        ("Video quality", "Qualité vidéo"),
        ("Audio quality", "Qualité audio"),
        ("Video profile", "Profil vidéo"),
        ("Audio profile", "Profil audio"),
        ("Metadata language", "Langue des métadonnées"),
        ("Catalogue language", "Langue du catalogue"),
        ("Catalog languages", "Langues du catalogue"),
        ("best available", "meilleure qualité disponible"),
        ("Automatic", "Automatique"),
        ("Auto", "Automatique"),
        ("Playback", "Lecture"),
        ("Manifest", "Manifeste"),
        ("Source", "Source"),
        ("Stream", "Flux"),
        ("Profile", "Profil"),
        ("Quality", "Qualité"),
        ("Resolution", "Résolution"),
        ("Codec", "Codec"),
        ("Language", "Langue"),
        ("Region", "Région"),
        ("Country", "Pays"),
        ("Account", "Compte"),
        ("Device", "Appareil"),
        ("device", "appareil"),
        ("Live", "Direct"),
        ("Market", "Marché"),
        ("Delivery", "Diffusion"),
        ("Requested", "Demandé"),
        ("Prefer", "Préférer"),
        ("Ask", "Demander"),
        ("Any", "Tous"),
        ("Main", "Principal"),
        ("Primary", "Principal"),
    ),
    "pt": (
        ("Auto / best available", "Automático / melhor disponível"),
        ("Best available", "Melhor disponível"),
        ("Manifest profile", "Perfil do manifesto"),
        ("Manifest resolution", "Resolução do manifesto"),
        ("Manifest color range", "Gama de cores do manifesto"),
        ("Manifest codec", "Codec do manifesto"),
        ("Playback source", "Fonte de reprodução"),
        ("Playback scenario", "Cenário de reprodução"),
        ("Authorization method", "Método de autorização"),
        ("Sign-in method", "Método de início de sessão"),
        ("Login method", "Método de início de sessão"),
        ("Browser cookies", "Cookies do navegador"),
        ("Apple ID account", "Conta Apple ID"),
        ("full ladder", "escada completa"),
        ("Dynamic range", "Gama dinâmica"),
        ("Source quality", "Qualidade da fonte"),
        ("Stream quality", "Qualidade do stream"),
        ("Video quality", "Qualidade do vídeo"),
        ("Audio quality", "Qualidade do áudio"),
        ("Video profile", "Perfil de vídeo"),
        ("Audio profile", "Perfil de áudio"),
        ("Metadata language", "Idioma dos metadados"),
        ("Catalogue language", "Idioma do catálogo"),
        ("Catalog languages", "Idiomas do catálogo"),
        ("best available", "melhor disponível"),
        ("Automatic", "Automático"),
        ("Auto", "Automático"),
        ("Playback", "Reprodução"),
        ("Manifest", "Manifesto"),
        ("Source", "Fonte"),
        ("Stream", "Stream"),
        ("Profile", "Perfil"),
        ("Quality", "Qualidade"),
        ("Resolution", "Resolução"),
        ("Codec", "Codec"),
        ("Language", "Idioma"),
        ("Region", "Região"),
        ("Country", "País"),
        ("Account", "Conta"),
        ("Device", "Dispositivo"),
        ("device", "dispositivo"),
        ("Live", "Ao vivo"),
        ("Market", "Mercado"),
        ("Delivery", "Entrega"),
        ("Requested", "Solicitado"),
        ("Prefer", "Preferir"),
        ("Ask", "Perguntar"),
        ("Any", "Qualquer"),
        ("Main", "Principal"),
        ("Primary", "Principal"),
    ),
}


_DYNAMIC_SETTING_PHRASES["zh-Hans"] += (
    ("Live recording length", "直播录制时长"),
    ("Offer the replay window", "提供回放窗口"),
    ("Check remote vaults before licensing", "授权前检查远程密钥库"),
    ("Store licensed keys in remote vaults", "将已授权密钥存入远程密钥库"),
    ("Dolby Vision + HDR10 hybrid output", "Dolby Vision + HDR10 混合输出"),
    ("Netflix video codec profile", "Netflix 视频编码配置"),
    ("Netflix video quality profile", "Netflix 视频质量配置"),
    ("Video", "视频"), ("Audio", "音频"), ("recording", "录制"), ("length", "时长"),
    ("Offer", "提供"), ("Replay", "回放"), ("window", "窗口"), ("Check", "检查"),
    ("remote vaults", "远程密钥库"), ("licensed keys", "已授权密钥"), ("Store", "存储"),
    ("before licensing", "授权前"), ("licensing", "授权"), ("hybrid output", "混合输出"),
)
_DYNAMIC_SETTING_PHRASES["zh-Hant"] += (
    ("Live recording length", "直播錄製時長"),
    ("Offer the replay window", "提供回放視窗"),
    ("Check remote vaults before licensing", "授權前檢查遠端金鑰庫"),
    ("Store licensed keys in remote vaults", "將已授權金鑰存入遠端金鑰庫"),
    ("Dolby Vision + HDR10 hybrid output", "Dolby Vision + HDR10 混合輸出"),
    ("Netflix video codec profile", "Netflix 視訊編碼設定"),
    ("Netflix video quality profile", "Netflix 視訊品質設定"),
    ("Video", "視訊"), ("Audio", "音訊"), ("recording", "錄製"), ("length", "時長"),
    ("Offer", "提供"), ("Replay", "回放"), ("window", "視窗"), ("Check", "檢查"),
    ("remote vaults", "遠端金鑰庫"), ("licensed keys", "已授權金鑰"), ("Store", "儲存"),
    ("before licensing", "授權前"), ("licensing", "授權"), ("hybrid output", "混合輸出"),
)
_DYNAMIC_SETTING_PHRASES["es"] += (
    ("Live recording length", "Duración de la grabación en directo"),
    ("Offer the replay window", "Ofrecer la ventana de repetición"),
    ("Check remote vaults before licensing", "Comprobar las bóvedas remotas antes de licenciar"),
    ("Store licensed keys in remote vaults", "Guardar las claves licenciadas en bóvedas remotas"),
    ("Dolby Vision + HDR10 hybrid output", "Salida híbrida Dolby Vision + HDR10"),
    ("Netflix video codec profile", "Perfil de códec de vídeo de Netflix"),
    ("Netflix video quality profile", "Perfil de calidad de vídeo de Netflix"),
    ("Video", "Vídeo"), ("Audio", "Audio"), ("recording", "grabación"), ("length", "duración"),
    ("Offer", "Ofrecer"), ("Replay", "repetición"), ("window", "ventana"), ("Check", "Comprobar"),
    ("remote vaults", "bóvedas remotas"), ("licensed keys", "claves licenciadas"), ("Store", "Guardar"),
    ("before licensing", "antes de licenciar"), ("licensing", "licencia"), ("hybrid output", "salida híbrida"),
)
_DYNAMIC_SETTING_PHRASES["fr"] += (
    ("Live recording length", "Durée de l'enregistrement en direct"),
    ("Offer the replay window", "Proposer la fenêtre de replay"),
    ("Check remote vaults before licensing", "Vérifier les coffres distants avant la licence"),
    ("Store licensed keys in remote vaults", "Enregistrer les clés licenciées dans les coffres distants"),
    ("Dolby Vision + HDR10 hybrid output", "Sortie hybride Dolby Vision + HDR10"),
    ("Netflix video codec profile", "Profil de codec vidéo Netflix"),
    ("Netflix video quality profile", "Profil de qualité vidéo Netflix"),
    ("Video", "Vidéo"), ("Audio", "Audio"), ("recording", "enregistrement"), ("length", "durée"),
    ("Offer", "Proposer"), ("Replay", "replay"), ("window", "fenêtre"), ("Check", "Vérifier"),
    ("remote vaults", "coffres distants"), ("licensed keys", "clés licenciées"), ("Store", "Enregistrer"),
    ("before licensing", "avant la licence"), ("licensing", "licence"), ("hybrid output", "sortie hybride"),
)
_DYNAMIC_SETTING_PHRASES["pt"] += (
    ("Live recording length", "Duração da gravação ao vivo"),
    ("Offer the replay window", "Oferecer a janela de repetição"),
    ("Check remote vaults before licensing", "Verificar cofres remotos antes da licença"),
    ("Store licensed keys in remote vaults", "Guardar chaves licenciadas em cofres remotos"),
    ("Dolby Vision + HDR10 hybrid output", "Saída híbrida Dolby Vision + HDR10"),
    ("Netflix video codec profile", "Perfil de codec de vídeo da Netflix"),
    ("Netflix video quality profile", "Perfil de qualidade de vídeo da Netflix"),
    ("Video", "Vídeo"), ("Audio", "Áudio"), ("recording", "gravação"), ("length", "duração"),
    ("Offer", "Oferecer"), ("Replay", "repetição"), ("window", "janela"), ("Check", "Verificar"),
    ("remote vaults", "cofres remotos"), ("licensed keys", "chaves licenciadas"), ("Store", "Guardar"),
    ("before licensing", "antes da licença"), ("licensing", "licença"), ("hybrid output", "saída híbrida"),
)


def _localized_dynamic_setting_text(text: str) -> str:
    """Translate common service-setting UI words when a provider has no catalog key."""
    locale = get_translator().locale
    phrases = _DYNAMIC_SETTING_PHRASES.get(locale)
    if not phrases or not text:
        return text
    result = text
    for source, target in sorted(phrases, key=lambda item: len(item[0]), reverse=True):
        result = re.sub(rf"(?<![A-Za-z]){re.escape(source)}(?![A-Za-z])", target, result, flags=re.IGNORECASE)
    return result


def tr(message_id: str, default: str | None = None, **values: Any) -> str:
    return get_translator().tr(message_id, default=default, **values)


def phrase(text: str, default: str | None = None, **values: Any) -> str:
    return get_translator().phrase(text, default=default, **values)


def setting_label(spec: Any) -> str:
    key = getattr(spec, "key", "")
    fallback = str(getattr(spec, "label", "") or key)
    return tr(f"setting.{key}", default=_localized_dynamic_setting_text(fallback))


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
    fallback = _localized_dynamic_setting_text(fallback)
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

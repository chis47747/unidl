"""The unidl application.

Global actions live here so every screen gets the same Back / Quit / Search /
Settings behaviour through the chrome bar and the same shortcuts.

Back-level rules, in one place:

1. **Screen 1, platforms** - Back clears the filter if there is one, otherwise
   nothing. It never pops, because it is the bottom of the stack.
2. **Screens 2 to 4** - Back answers the pending ask with "back", and the flow
   decides what that means: usually the previous ask, which may live on a
   different screen. When nothing is pending, screen 2 leaves the service,
   screen 3 cancels the in-flight flow and reveals screen 2, and screen 4
   cancels a running download as it returns to the previous page.
3. **Overlays** (settings, search) - Back closes them and returns to whatever
   opened them.

Screens opt in by implementing ``go_back() -> bool``: return True if handled
internally, False to be popped.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from textual.app import App
from textual.binding import Binding
from textual.screen import ModalScreen

from ..core import i18n, playready, service_catalog, vaults
from ..core.brands import service_tag
from ..core.config import Config
from ..core.credentials import mask_in
from ..core.devreload import ReloadError, ReloadResult, ServiceReloader
from ..core.drm import CdmError
from ..core.engine import Engine
from ..core.helpers import HelperResolver
from ..core.i18n import tr
from ..core.service import Capabilities, Service, registry
from ..core.settings import SettingsStore, global_settings
from ..core.vault import KeyVault
from .bidi import (
    EXPLICIT_BIDI_ENTER,
    EXPLICIT_BIDI_EXIT,
    install_textual_rtl_shaping,
    rtl_terminal_mode,
)
from .clipboard import write_system_clipboard
from .home import HomeScreen
from .palette import UnidlCommands
from .terminal_progress import (
    DEFAULT_COMPLETION_ALERT,
    DEFAULT_TERMINAL_TITLE,
    OSC_PROGRESS_CLEAR,
    PROGRESS_HIDDEN,
    normalize_completion_alert,
    osc_notify_sequence,
    osc_progress_sequence,
    osc_title_sequence,
    supports_osc_progress,
    supports_osc_title,
)
from .theme import (
    ALL_THEMES,
    CSS,
    DEFAULT_ACCENT,
    DEFAULT_DENSITY,
    DEFAULT_SEMANTIC_COLORS,
    DEFAULT_TITLE_COLOR,
    THEME_NAMES,
    Palette,
    normalize_accent,
    normalize_density,
    normalize_semantic_colors,
    normalize_theme,
    normalize_title_color,
    palette_for,
    textual_theme,
    theme_name_for,
    title_color_stops,
)

#: quitting is destructive, so it needs a second press within this window.
QUIT_CONFIRM_WINDOW = 1.5

# Closing sockets and terminating downloader process groups is immediate, but a
# Python worker still needs a moment to unwind its ``finally`` blocks.  Keep the
# whole application wait bounded so an uncooperative service cannot trap exit.
SESSION_SHUTDOWN_TIMEOUT = 5.0
PROCESS_SHUTDOWN_TIMEOUT = 8.0

#: Below this many rows the roomy spacing is given up - see the "short screen"
#: block at the end of :mod:`unidl.tui.theme`. Twenty-eight leaves three rows of
#: platforms under the banner with every row of air still in place; below it the
#: air is what has to go, because the list is the screen and the space around it
#: is not.
ROOMY_ROWS = 28

#: How many recently opened services appear in the Home quick-launch bar.
MAX_RECENT_SERVICES = 5
RECENT_SERVICES_KEY = "recent_services"

_SERVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _portable_import_service_class(document) -> type[Service]:
    """A network-free Service-shaped context for one portable export.

    Session and Engine consume a small Service contract for settings, naming and
    lifecycle hooks. An import must not turn that implementation detail into an
    installation, account, helper or licence dependency, so this class keeps only
    inert identity metadata from the document and inherits Core's no-op hooks.
    """
    raw_id = str(getattr(document, "service", "") or "").strip().lower()
    source_id = raw_id
    if not _SERVICE_ID.fullmatch(source_id):
        source_id = "portable-import"
    third_party = not getattr(document, "is_native", True)
    context_id = "third-party-import" if third_party else source_id
    source_name = str(getattr(document, "service_name", "") or raw_id or "Portable import")
    has_live = any(bool(getattr(entry, "is_live", False)) for entry in document.entries)

    def missing_keys(self, playback):
        del self
        drm = getattr(playback, "drm", None)
        inventory = getattr(drm, "context", {}).get("license_tracks") if drm is not None else None
        if inventory is not None and not inventory:
            # Older version-1 exports did not persist DrmInfo.clear. The parsed
            # ladder is authoritative on replay: if it has no encrypted tracks,
            # there is nothing for a KID:key to unlock.
            return []
        raise CdmError(
            "This import declares license-bound DRM but contains no usable KID:key. "
            "Clear DASH/HLS, direct media and playlist AES-128 do not need an exported "
            "content key; Widevine, PlayReady and similar CENC streams do. No service "
            "or license request was attempted; create a complete export again."
        )

    def exported_manifest_variants(self, playback, log):
        """Expose already-authorized export URLs without contacting a service."""
        del self, log
        return [
            replace(
                playback,
                manifest_url=url,
                json_manifest=None,
                alternate_manifest_urls=(),
                merge_manifests=False,
            )
            for url in playback.alternate_manifest_urls
            if str(url).strip()
        ]

    return type(
        "PortableImportService",
        (Service,),
        {
            "__module__": __name__,
            # Foreign data gets a dedicated state namespace.  Using its source
            # service ID here would make Registry.build point at that service's
            # token/settings directories even though no service class is loaded.
            "ID": context_id,
            "NAME": source_name,
            "TAG": service_tag(raw_id),
            "USES": Capabilities().with_self("drm"),
            "SUPPORTS_URL": False,
            "SUPPORTS_SEARCH": False,
            "SUPPORTS_LIVE": has_live,
            "_PORTABLE_IMPORT_FALLBACK": True,
            "_THIRD_PARTY_IMPORT": third_party,
            "_NO_LICENSE_IMPORT": third_party,
            "_EXPORT_SOURCE_ID": source_id,
            "get_keys": missing_keys,
            "manifest_variants": exported_manifest_variants,
        },
    )

# Textual's Linux driver emits these modes while it owns the terminal.  Its
# normal shutdown path turns them off, but an SSH hangup or a process signal can
# bypass that path and leave iTerm in mouse-reporting mode.  Keep this cleanup
# deliberately independent from Textual so it also covers a renderer failure.
_TERMINAL_RESET = (
    f"\x1b[0m\x1b[<u{EXPLICIT_BIDI_EXIT}{OSC_PROGRESS_CLEAR}\x1b[?7h\x1b[?47l\x1b[?1047l\x1b[?1049l\x1b[?25h"
    "\x1b[?1004l\x1b[?2004l\x1b[?2026l\x1b[?2048l"
    # Disable reporting on the restored primary screen, after leaving every
    # alternate-screen mode. Include UTF-8 mouse mode as well as SGR.
    "\x1b[?9l\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1016l"
)


def _restore_terminal_state(stream=None, *, clear_input: bool = False) -> None:
    """Best-effort reset for terminal modes UniDL/Textual may have enabled."""

    if stream is None:
        stream = getattr(sys, "__stdout__", None) or getattr(sys, "stdout", None)
    if stream is None or getattr(stream, "closed", False):
        return
    try:
        if sys.platform == "win32":
            from .windows_driver import restore_terminal_state_windows

            restore_terminal_state_windows(stream, _TERMINAL_RESET, clear_input=clear_input)
            return
        stream.write(_TERMINAL_RESET)
        stream.flush()
    except (AttributeError, OSError, ValueError):
        # The SSH fd may already be gone. There is no useful recovery to do in
        # that case, and cleanup must never hide the original shutdown path.
        pass


class _TerminalStateGuard:
    """Restore terminal modes even when the process receives a hangup."""

    _SIGNAL_NAMES = ("SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM")

    def __init__(self, shutdown=None) -> None:
        self._previous: dict[int, object] = {}
        self._shutdown = shutdown
        # Signal handlers run on the main thread, but a second Ctrl+C may be
        # delivered while the first handler is closing sockets and worker
        # resources.  Keep that second delivery from recursively calling the
        # previous handler (which is usually ``default_int_handler``).
        self._handling_signal = False

    def __enter__(self):
        for name in self._SIGNAL_NAMES:
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            except (OSError, ValueError):
                # Signals can be unavailable on a platform or from a non-main
                # thread. Textual still owns its regular cleanup in that case.
                continue
        atexit.register(_restore_terminal_state, clear_input=True)
        return self

    def _handle_signal(self, signum: int, frame) -> None:
        if self._handling_signal:
            # The first handler has already requested an orderly exit.  A
            # repeated interrupt must not re-enter cleanup or raise another
            # KeyboardInterrupt while asyncio is shutting its executor down.
            return
        self._handling_signal = True
        try:
            if callable(self._shutdown):
                try:
                    self._shutdown()
                except Exception:
                    pass
            _restore_terminal_state(clear_input=True)

            # Ctrl+C is an application request to stop, not an unhandled
            # exception.  ``run`` supplies a callback that aborts sessions and
            # asks Textual to leave its event loop, so propagating
            # ``default_int_handler`` here only creates a traceback and can
            # interrupt asyncio's executor shutdown.
            if signum == getattr(signal, "SIGINT", None):
                return

            previous = self._previous.get(signum, signal.SIG_DFL)
            if previous is signal.SIG_IGN:
                return
            if callable(previous):
                previous(signum, frame)
                return
            # Re-raise the original default signal action after the terminal is
            # usable again. ``os._exit`` would skip every other interpreter cleanup.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        finally:
            self._handling_signal = False

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        try:
            atexit.unregister(_restore_terminal_state)
        except Exception:
            pass
        _restore_terminal_state(clear_input=True)
        for signum, previous in self._previous.items():
            try:
                signal.signal(signum, previous)
            except (OSError, ValueError):
                pass


class UnidlApp(App):
    CSS = CSS
    TITLE = "unidl"
    COMMANDS = {UnidlCommands}
    DEFAULT_MODE = "home"

    BINDINGS = [
        ("ctrl+q", "global_quit", "Quit"),
        # a chord, so it still works with a text field focused
        ("ctrl+t", "toggle_theme", "Theme"),
        # Do not set priority=True at app level: modal editors own Ctrl+S for
        # Save/Apply via screen-level priority bindings, while Input/TextArea do
        # not consume Ctrl+S so global settings still fires from text fields.
        Binding(
            "ctrl+s",
            "global_settings",
            "Settings",
            show=False,
        ),
        Binding("question_mark", "show_shortcuts", "Shortcuts", show=False),
    ]
    def __init__(self, config: Config | None = None):
        super().__init__()
        self.config = config or Config.load()
        self.config.paths.ensure()
        self.registry = registry
        self._registry_identity = id(self.registry)
        self.settings_store = SettingsStore(self.config.paths.home / "settings.json")
        self.globals = global_settings(self.settings_store, config=self.config)
        self._home_interface_modes: dict[bool, str] = {}
        self._registered_service_ids, self._home_service_ids = service_catalog.ensure_state(
            self.settings_store, (service.ID for service in self.registry.all())
        )
        self.vault = KeyVault(self.config.paths.keys_db)
        self.vaults = vaults.build(self.config, local=self.vault)
        self._helper_cache: dict[str, tuple[str, bool]] = {}
        self._account_cache: dict[str, tuple[str, bool]] = {}
        # Created lazily because the small headless checks replace ``registry``
        # with an isolated one after constructing the app. Production keeps the
        # process-wide registry, while both routes still reload the one in use.
        self._service_reloader: ServiceReloader | None = None
        self._resolver = HelperResolver(self.config)
        self._quit_armed_at: float | None = None
        # A worker can outlive its screens while the native downloader is still
        # active, so shutdown follows controller ownership rather than the UI
        # stack alone.
        self._session_controllers: dict[int, object] = {}
        # a device chosen in a previous run lives in settings.json, not the
        # config file, so it has to be handed to the resolver on the way up
        self.apply_device_choice()
        self._preview_theme: str | None = None
        self._preview_accent: str | None = None
        self._preview_title_color: str | None = None
        self._preview_density: str | None = None
        self._preview_semantic_colors: str | None = None
        self._last_dark_theme: str = (
            self.theme_preference
            if palette_for(self.theme_preference).dark
            else "dark"
        )
        self._terminal_progress_state: tuple[int, int, str] | None = None
        # Themes have to be registered and selected here, not in on_mount: the
        # stylesheet is parsed before then, and it refers to this theme's own
        # variables, which would be undefined under the default one.
        for theme in ALL_THEMES.values():
            self.register_theme(theme)
        install_textual_rtl_shaping()
        self.apply_theme()
        self.apply_locale()

    def register_session(self, controller) -> None:
        self._session_controllers[id(controller)] = controller

    def unregister_session(self, controller) -> None:
        self._session_controllers.pop(id(controller), None)

    def has_active_session(self) -> bool:
        return any(
            getattr(screen, "controller", None) is not None
            for screen in getattr(self, "screen_stack", ())
        )

    def _sync_terminal_bidi(self, *, enable_explicit: bool = True) -> None:
        """Toggle ECMA-48 explicit BiDi mode on interactive terminals.

        BiDi-capable terminals (iTerm2 3.5+, VTE/GNOME Terminal, Konsole,
        WezTerm) otherwise run row-wide UAX #9 reordering over full-screen TUI
        rows, double-reversing pre-shaped Arabic and scrambling text across box
        borders. macOS Terminal.app ignores these escapes and instead freezes
        rows via zero-width ``U+1160`` LTR anchors at compositor emission time.
        Headless test drivers and ``UNIDL_DISABLE_RTL_SHAPING=1`` /
        ``UNIDL_RTL_MODE=off`` skip entering explicit mode.
        """
        if getattr(self, "is_headless", False):
            return
        driver = getattr(self, "_driver", None)
        if driver is None or not hasattr(driver, "write"):
            return
        mode = rtl_terminal_mode()
        if mode == "apple_terminal":
            return
        sequence = (
            EXPLICIT_BIDI_ENTER
            if (enable_explicit and mode == "explicit")
            else EXPLICIT_BIDI_EXIT
        )
        try:
            driver.write(sequence)
            if hasattr(driver, "flush"):
                driver.flush()
        except Exception:
            pass

    def set_terminal_progress(
        self,
        state: int,
        percent: int = 0,
        *,
        title: str = DEFAULT_TERMINAL_TITLE,
    ) -> None:
        """Emit OSC 9;4 native progress and OSC 0 window title when changed."""
        if getattr(self, "is_headless", False):
            return
        driver = getattr(self, "_driver", None)
        if driver is None or not hasattr(driver, "write"):
            return
        clamped = max(0, min(100, int(percent)))
        clean_title = title or DEFAULT_TERMINAL_TITLE
        key = (int(state), clamped, clean_title)
        if key == getattr(self, "_terminal_progress_state", None):
            return
        self._terminal_progress_state = key
        parts: list[str] = []
        if supports_osc_progress():
            parts.append(osc_progress_sequence(state, clamped))
        if supports_osc_title():
            parts.append(osc_title_sequence(clean_title))
        if not parts:
            return
        try:
            driver.write("".join(parts))
            if hasattr(driver, "flush"):
                driver.flush()
        except Exception:
            pass

    def clear_terminal_progress(self) -> None:
        """Hide any OSC 9;4 terminal progress bar and restore default title."""
        self.set_terminal_progress(PROGRESS_HIDDEN, 0, title=DEFAULT_TERMINAL_TITLE)

    def emit_completion_alert(self, title: str, body: str = "") -> bool:
        """Emit an optional terminal completion notification (OSC 9/99/777 or BEL)."""
        mode = normalize_completion_alert(
            self.globals.get("completion_alert", DEFAULT_COMPLETION_ALERT)
        )
        if mode == "off" or getattr(self, "is_headless", False):
            return False
        driver = getattr(self, "_driver", None)
        if driver is None or not hasattr(driver, "write"):
            return False
        sequence = osc_notify_sequence(title, body, mode=mode)
        if not sequence:
            return False
        try:
            driver.write(sequence)
            if hasattr(driver, "flush"):
                driver.flush()
            return True
        except Exception:
            return False

    def action_show_shortcuts(self) -> None:
        """Toggle the contextual shortcut cheat sheet unless typing in a field."""
        from textual.widgets import Input as TextualInput
        from textual.widgets import TextArea as TextualTextArea

        from .shortcuts_screen import ShortcutsScreen

        if isinstance(self.screen, ShortcutsScreen):
            self.pop_screen()
            return
        if isinstance(self.focused, (TextualInput, TextualTextArea)):
            return
        self.push_screen(ShortcutsScreen(self.screen))

    def on_mount(self) -> None:
        # Keep initial composition on Textual's get_default_screen path, which
        # puts the root on its stack before mounting children. In Textual 8.2,
        # a pre-registered named mode mounts its children first; run_async's
        # eager tasks then let Input access an empty stack. Register only after
        # startup so switch_mode can safely return to this already-mounted root.
        self._sync_terminal_bidi(enable_explicit=True)
        self.apply_layout_density()
        self.add_mode(self.DEFAULT_MODE, self.get_default_screen)

    def on_app_focus(self) -> None:
        self._sync_terminal_bidi(enable_explicit=True)

    def exit(self, *args, **kwargs):  # noqa: D102 - Textual lifecycle override
        arm_deadline = getattr(self, "_arm_exit_deadline", None)
        if arm_deadline is not None:
            arm_deadline()
        self.abort_sessions()
        return super().exit(*args, **kwargs)

    async def on_unmount(self) -> None:
        """Finish session cleanup without blocking Textual's UI thread.

        ``App.exit`` runs inside the message loop.  Joining a synchronous
        downloader there would prevent worker callbacks from being serviced and
        could turn a clean cancellation into a timeout.  Textual dispatches this
        hook during its async shutdown phase, so the bounded wait runs in a
        helper thread while the loop remains able to drain those callbacks.
        """
        self.clear_terminal_progress()
        self._sync_terminal_bidi(enable_explicit=False)
        self.abort_sessions()
        await asyncio.to_thread(self.wait_for_sessions, SESSION_SHUTDOWN_TIMEOUT)

    def get_default_screen(self) -> HomeScreen:
        # HomeScreen is the bottom of the stack, so pop_screen is always safe
        if self.globals.get("beta_interface", False):
            from .beta_home import BetaHomeScreen

            return BetaHomeScreen()
        return HomeScreen()

    def refresh_home_interface(self) -> None:
        """Switch the root only while home is active, never interrupt a session."""
        from .beta_home import BetaHomeScreen

        if len(self.screen_stack) != 1 or not isinstance(self.screen, HomeScreen):
            return
        wanted = bool(self.globals.get("beta_interface", False))
        current = isinstance(self.screen, BetaHomeScreen)
        self._home_interface_modes.setdefault(current, self.current_mode)
        if wanted != current:
            mode = self._home_interface_modes.get(wanted)
            if mode is None:
                mode = "home-beta" if wanted else "home-classic"
                self.add_mode(mode, BetaHomeScreen if wanted else HomeScreen)
                self._home_interface_modes[wanted] = mode
            self.switch_mode(mode)

    def on_resize(self, event) -> None:
        """Trade the spacing for the content when the window is short.

        On the app rather than on each screen: the class is what the stylesheet
        keys off, so setting it here makes every screen - including the ones with
        no resize handler of their own - agree about how much room there is. The
        alternative was five screens each deciding for themselves, which is five
        chances for two of them to disagree while both are on the stack.
        """
        self.apply_layout_density(height=event.size.height)

    def notify(  # noqa: D102 - overrides Textual's own, documented there
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = False,
    ) -> None:
        """Show a toast, as plain text.

        Every toast in this app is a sentence with a value in it - a file name, a
        path, a URL, a save name, an exception. Textual reads a toast as markup by
        default, so ``[WEBDL-1080p]`` disappeared from the message and a value
        containing ``[/]`` raised out of the render. Both are ordinary inputs here.

        The flag is ignored rather than honoured: this is the one funnel every
        ``self.notify`` in every screen passes through (``Widget.notify`` forwards
        to it), and a per-call opt-in would only be a way to reintroduce the bug.
        """
        del markup
        super().notify(
            message,
            title=title,
            severity=severity,  # type: ignore[arg-type]
            **({} if timeout is None else {"timeout": timeout}),
            markup=False,
        )

    @property
    def services(self) -> list[type[Service]]:
        self._sync_registry_state()
        return [service for service in self.registry.all() if service.ID in self._registered_service_ids]

    @property
    def home_services(self) -> list[type[Service]]:
        """Registered services selected for the homepage grid."""

        return [service for service in self.services if service.ID in self._home_service_ids]

    def set_home_services(self, service_ids) -> None:
        """Apply a homepage selection immediately for the current process."""

        chosen = {str(value).strip().lower() for value in service_ids if str(value).strip()}
        self._home_service_ids = chosen & self._registered_service_ids

    def recent_service_ids(self) -> tuple[str, ...]:
        """Up to ``MAX_RECENT_SERVICES`` recently opened registered service IDs."""
        self._sync_registry_state()
        raw = self.settings_store.values_for(service_catalog.GLOBAL_SCOPE).get(
            RECENT_SERVICES_KEY
        )
        if not isinstance(raw, list):
            return ()
        seen: set[str] = set()
        result: list[str] = []
        for item in raw:
            sid = str(item or "").strip().lower()
            if not sid or sid in seen or sid not in self._registered_service_ids:
                continue
            seen.add(sid)
            result.append(sid)
            if len(result) >= MAX_RECENT_SERVICES:
                break
        return tuple(result)

    def recent_services(self) -> list[type[Service]]:
        """Registered services opened most recently, in MRU order."""
        by_id = {service.ID: service for service in self.services}
        return [by_id[sid] for sid in self.recent_service_ids() if sid in by_id]

    def record_recent_service(self, service_cls: type[Service]) -> None:
        """Persist ``service_cls`` at the front of the MRU list."""
        if getattr(service_cls, "_PORTABLE_IMPORT_FALLBACK", False):
            return
        self._sync_registry_state()
        sid = str(getattr(service_cls, "ID", "") or "").strip().lower()
        if not sid or sid not in self._registered_service_ids:
            return
        existing = list(self.recent_service_ids())
        updated = [sid, *(item for item in existing if item != sid)][:MAX_RECENT_SERVICES]
        if updated == existing:
            return
        self.settings_store.put(service_catalog.GLOBAL_SCOPE, RECENT_SERVICES_KEY, updated)
        self.settings_store.save()

    def _sync_registry_state(self) -> None:
        """Keep headless/test registry replacements usable without persistence churn.

        Production keeps one process-wide registry.  Tests and developer tools
        may replace ``app.registry`` with an isolated registry after app
        construction; its services should retain the pre-registration default
        for that ephemeral process rather than being filtered by another
        install's saved IDs.
        """
        identity = id(self.registry)
        if identity == self._registry_identity:
            return
        current = {str(service.ID).strip().lower() for service in self.registry.all()}
        self._registry_identity = identity
        self._registered_service_ids = current
        self._home_service_ids = set(current)

    # ------------------------------------------------------------------- hints
    def account_hint(self, service_cls: type[Service]) -> tuple[str, bool]:
        """Login summary for the main screen, without doing any network work.

        Cached: the list is rebuilt on every filter keystroke and there are
        the service registry, so probing each one every time is not free.
        """
        cached = self._account_cache.get(service_cls.ID)
        if cached is not None:
            return cached
        try:
            probe = self.registry.build(
                service_cls, self.config, self.settings_store, globals_scope=self.globals
            )
            status = probe.auth_status()
            hint = (mask_in(status.label), status.logged_in or status.anonymous_ok)
        except Exception as exc:  # a broken service must not break the list
            hint = (f"unavailable ({type(exc).__name__})", False)
        self._account_cache[service_cls.ID] = hint
        return hint

    def invalidate_hints(self, service_id: str | None = None) -> None:
        """Drop cached login state after a login, logout or settings change."""
        if service_id is None:
            self._account_cache.clear()
        else:
            self._account_cache.pop(service_id, None)

    def helper_hint(self, service_cls: type[Service]) -> tuple[str, bool]:
        if not service_cls.HELPERS:
            return "-", None  # type: ignore[return-value]
        if service_cls.ID not in self._helper_cache:
            report = self._resolver.report(service_cls.ID, service_cls.HELPERS)
            self._helper_cache[service_cls.ID] = (report.summary(), report.ready)
        return self._helper_cache[service_cls.ID]

    # ---------------------------------------------------------- developer reload
    def reload_service_code(self, service_cls: type[Service]) -> ReloadResult:
        """Replace registry classes at the one explicit, development-only seam.

        This is intentionally callable only from the bare home screen. A service
        session owns API clients, licences and playback lifecycle state; replacing
        its class while it is live would create a half-old, half-new session. The
        old instance is therefore never patched, and only the next build observes
        the replacement class registered by :class:`ServiceReloader`.
        """
        if not bool(self.globals.get("debug", False)):
            raise ReloadError("turn on Debug mode in global Settings first")
        if not isinstance(self.screen, HomeScreen) or len(self.screen_stack) != 1:
            raise ReloadError("service code can only be reloaded from the main screen")

        reloader = self._service_reloader
        if reloader is None or reloader.registry is not self.registry:
            reloader = self._service_reloader = ServiceReloader(self.registry)
        result = reloader.reload(service_cls)

        for service_id in result.services:
            self._account_cache.pop(service_id, None)
            self._helper_cache.pop(service_id, None)
        # A changed declaration may point the same helper key at a different
        # file. Existing sessions retain their resolved report; the next service
        # build and the home-screen readiness survey must look again.
        self._resolver.forget()
        from ..core import readiness

        readiness.forget()
        return result

    # ----------------------------------------------------------------- launching
    def open_service(
        self,
        service_cls: type[Service],
        target: str | None = None,
        search: str | None = None,
        *,
        replace_screen: object | None = None,
    ) -> bool:
        from .session import SessionController

        if any(getattr(screen, "controller", None) is not None for screen in getattr(self, "screen_stack", ())):
            self.notify(
                tr("notify.session_active", default="Leave the current session before opening another."),
                severity="warning",
                timeout=4,
            )
            return False

        service = self.registry.build(
            service_cls, self.config, self.settings_store, globals_scope=self.globals
        )
        report = service.ctx.helpers
        if not report.ready:
            self.notify(report.blocking_message(), title=tr("notify.needs_setup", name=service_cls.NAME),
                        severity="error", timeout=10)
            return False
        if target:
            service.ctx.extras["initial_target"] = target
        if search:
            service.ctx.extras["initial_search"] = search
        controller = SessionController(
            self,
            service,
            Engine(
                self.config,
                vault=self.vault,
                vault_collection=self.vaults,
            ),
        )
        self.register_session(controller)
        self.record_recent_service(service_cls)
        if replace_screen is not None and self.screen is replace_screen:
            self.pop_screen()
        self.push_screen(controller.start_screen())
        return True

    def open_import(self, document) -> bool:
        """Finish what an export file already resolved. Returns whether it started.

        A native UniDL export may retain an installed platform's custom delivery
        hooks. A third-party export always uses the inert generic context: matching
        its source label to an installed service must never load that service's
        account, cookies, tokens or licence transport.
        """
        from .session import SessionController

        if any(getattr(screen, "controller", None) is not None for screen in getattr(self, "screen_stack", ())):
            self.notify(
                tr("notify.session_active", default="Leave the current session before opening another."),
                severity="warning",
                timeout=4,
            )
            return False

        installed = (
            self.registry.for_id(document.service)
            if getattr(document, "is_native", True)
            else None
        )
        service_cls = installed or _portable_import_service_class(document)
        service = self.registry.build(
            service_cls, self.config, self.settings_store, globals_scope=self.globals
        )
        if installed is not None and not service.ctx.helpers.ready:
            self.notify(
                service.ctx.helpers.blocking_message(),
                title=tr("notify.needs_setup", name=service_cls.NAME),
                severity="error",
                timeout=10,
            )
            return False
        service.ctx.extras["initial_import"] = document
        controller = SessionController(
            self,
            service,
            Engine(
                self.config,
                vault=self.vault,
                vault_collection=self.vaults,
            ),
        )
        controller.import_session = True
        self.register_session(controller)
        self.push_screen(controller.start_screen())
        return True

    def open_import_queue(self, items) -> bool:
        """Start one serial queue without merging independent export documents."""
        from ..core import exports
        from .import_queue import ImportQueueController

        if any(getattr(screen, "controller", None) is not None for screen in getattr(self, "screen_stack", ())):
            self.notify(
                tr("notify.session_active", default="Leave the current session before opening another."),
                severity="warning",
                timeout=4,
            )
            return False

        items = tuple(items)
        if not items:
            self.notify(tr("import.queue.empty"), severity="warning")
            return False
        # This neutral envelope is only a batch count/worker entry point. Each
        # source retains its own format, settings, manifests, keys and hooks.
        envelope = exports.Document(
            service="mixed-import", service_name=tr("import.queue.title"),
            entries=[item.entry for item in items],
        )
        service = self.registry.build(
            _portable_import_service_class(envelope), self.config,
            self.settings_store, globals_scope=self.globals,
        )
        service.ctx.extras["initial_import"] = envelope
        controller = ImportQueueController(
            self, service,
            Engine(self.config, vault=self.vault, vault_collection=self.vaults),
            items,
        )
        self.register_session(controller)
        self.push_screen(controller.start_screen())
        return True

    # ------------------------------------------------------------ global actions
    def action_global_back(self) -> None:
        screen = self.screen
        handler = getattr(screen, "go_back", None)
        if callable(handler) and handler():
            return
        if len(self.screen_stack) > 1:
            self.pop_screen()

    @property
    def playready_ready(self) -> bool:
        return playready.available()

    # --------------------------------------------------------------- clipboard
    def on_mouse_up(self, event) -> None:
        """Copy whatever was just selected with the mouse.

        Textual tracks the selection but does not copy it, so releasing the
        button after a drag is where that happens, allowing it to be pasted
        straight into another window.
        """
        try:
            text = self.screen.get_selected_text()
        except Exception:
            return
        if not text or not text.strip():
            return
        if self.copy_text(text) is False:
            return
        shown = text.strip().splitlines()[0][:60]
        self.notify(tr("notify.copied", text=shown), timeout=2)

    def copy_text(self, text: str) -> bool:
        """Copy natively; terminal escape sequences alone are not confirmation."""
        sent_to_terminal = False
        try:
            self.copy_to_clipboard(text)
            sent_to_terminal = self._driver is not None
        except Exception:
            pass
        if write_system_clipboard(text):
            return True
        self.notify(
            tr("notify.copy_unconfirmed" if sent_to_terminal else "notify.copy_failed"),
            severity="warning", timeout=5,
        )
        return False

    def open_url(self, url: str) -> None:
        """Hand a link to the browser, for activation pages you would have to type.

        The same opener as :meth:`open_path`, kept separate because there is
        nothing to say about a URL's ``name`` and because a link that cannot be
        opened has to be readable instead - over SSH the browser that answers is
        not on the screen you are looking at.
        """
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            valid = parsed.scheme in {"http", "https"} and host and not any(c.isspace() for c in host)
            if valid:
                # Accessing port catches invalid port values left lazy by urlsplit.
                _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            self.notify(
                tr("notify.invalid_url", default="Expected a valid HTTP or HTTPS link."),
                title=tr("notify.open_this"), severity="warning", timeout=6,
            )
            return
        in_ssh = bool(
            os.environ.get("SSH_CONNECTION")
            or os.environ.get("SSH_CLIENT")
            or os.environ.get("SSH_TTY")
        )
        if sys.platform == "win32":
            if in_ssh:
                self.notify(url, title=tr("notify.open_this"), timeout=12)
                return
            try:
                opened = webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                self.notify(f"{url}\n({exc})", title=tr("notify.open_this"), severity="warning", timeout=12)
                return
            if opened:
                self.notify(tr("notify.opened_browser"), timeout=3)
            else:
                self.notify(url, title=tr("notify.open_this"), timeout=12)
            return
        headless_linux = sys.platform != "darwin" and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )
        if in_ssh or headless_linux:
            self.notify(url, title=tr("notify.open_this"), timeout=12)
            return
        opener_name = "open" if sys.platform == "darwin" else "xdg-open"
        opener = shutil.which(opener_name)
        if opener is None:
            self.notify(url, title=tr("notify.open_this"), timeout=12)
            return
        try:
            subprocess.Popen(  # noqa: S603
                [opener, url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.notify(tr("notify.opened_browser"), timeout=3)
        except OSError as exc:
            self.notify(f"{url}\n({exc})", title=tr("notify.open_this"), severity="warning", timeout=12)

    # ------------------------------------------------------------------ config
    def open_path(self, path: Path) -> None:
        """Hand a file or folder to whatever the OS opens it with."""
        if sys.platform == "win32":
            startfile = getattr(os, "startfile", None)
            if callable(startfile):
                try:
                    startfile(str(path))
                    self.notify(tr("notify.opened_name", name=path.name), timeout=3)
                    return
                except OSError as exc:
                    self.notify(tr("notify.open_fail", path=path, error=exc), severity="error", timeout=6)
                    return
            opener_name = "explorer"
        elif sys.platform == "darwin":
            opener_name = "open"
        else:
            opener_name = "xdg-open"
        opener = shutil.which(opener_name)
        if opener is None:
            self.notify(str(path), title=tr("notify.path"), timeout=8)
            return
        try:
            subprocess.Popen(  # noqa: S603
                [opener, str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.notify(tr("notify.opened_name", name=path.name), timeout=3)
        except OSError as exc:
            self.notify(tr("notify.open_fail", path=path, error=exc), severity="error", timeout=6)

    def open_config(self) -> None:
        """Open the config file in the system default editor, creating it if needed."""
        try:
            path, created = self.config.ensure_config_file()
            if created:
                self.notify(tr("notify.created", path=path), timeout=4)
        except (OSError, ValueError) as exc:
            self.notify(tr("notify.create_fail", path=self.config.config_path, error=exc), severity="error", timeout=6)
            return
        self.open_path(path)

    # ------------------------------------------------------------------- theme
    @property
    def theme_preference(self) -> str:
        """The selected palette, normalising legacy ``system`` values to dark."""
        return normalize_theme(self.globals.get("theme", "dark"))

    @property
    def theme_mode(self) -> str:
        """The concrete palette currently in use (including live preview)."""
        return self._preview_theme or self.theme_preference

    @property
    def accent_preference(self) -> str:
        """The persisted system accent colour preset."""
        return normalize_accent(self.globals.get("accent_color", DEFAULT_ACCENT))

    @property
    def accent_mode(self) -> str:
        """The active system accent preset (including live preview)."""
        return self._preview_accent or self.accent_preference

    @property
    def title_color_preference(self) -> str:
        """The persisted title/banner colour preset."""
        return normalize_title_color(self.globals.get("title_color", DEFAULT_TITLE_COLOR))

    @property
    def title_color_mode(self) -> str:
        """The active title/banner colour preset (including live preview)."""
        return self._preview_title_color or self.title_color_preference

    @property
    def density_preference(self) -> str:
        """The persisted layout density mode (auto, comfortable, or compact)."""
        return normalize_density(self.globals.get("layout_density", DEFAULT_DENSITY))

    @property
    def density_mode(self) -> str:
        """The active layout density mode (including live preview)."""
        return self._preview_density or self.density_preference

    @property
    def semantic_colors_preference(self) -> str:
        """The persisted semantic status color mode (default or colorblind)."""
        return normalize_semantic_colors(
            self.globals.get("semantic_colors", DEFAULT_SEMANTIC_COLORS)
        )

    @property
    def semantic_colors_mode(self) -> str:
        """The active semantic status color mode (including live preview)."""
        return self._preview_semantic_colors or self.semantic_colors_preference

    def apply_layout_density(self, *, height: int | None = None) -> None:
        """Apply compact/comfortable/auto spacing across the app and mounted screens."""
        mode = self.density_mode
        if mode == "compact":
            is_short = True
        elif mode == "comfortable":
            is_short = False
        else:
            rows = height if height is not None else getattr(self.size, "height", 0)
            is_short = bool(rows and rows < ROOMY_ROWS)
        self.set_class(is_short, "short")
        if not self.is_running:
            return
        for screen in self._all_live_screens():
            screen.set_class(is_short, "short")
            fit_beta = getattr(screen, "_fit_beta", None)
            if callable(fit_beta):
                try:
                    fit_beta()
                except Exception:
                    pass
            resize_spacing = getattr(screen, "_resize_spacing", None)
            if callable(resize_spacing):
                try:
                    resize_spacing()
                except Exception:
                    pass
            refresh_banner = getattr(screen, "refresh_banner", None)
            if callable(refresh_banner):
                try:
                    refresh_banner()
                except Exception:
                    pass
            try:
                screen.refresh(layout=True)
            except Exception:
                pass

    def title_banner_colour(self) -> str | tuple[str, ...]:
        """Colour token or gradient stops for the ``unidl`` and service banners."""
        stops = title_color_stops(
            self.title_color_mode,
            dark=self.palette.dark,
            accent=self.accent_mode,
        )
        if self.title_color_mode == DEFAULT_TITLE_COLOR and len(stops) == 1:
            return "$accent"
        return stops

    @property
    def palette(self) -> Palette:
        """Live colour values, for Rich renderables the stylesheet cannot reach.

        Anything written into the log is a Rich ``Text``, and Rich resolves
        styles itself without knowing about Textual's design tokens, so those
        call sites need the numbers.
        """
        return palette_for(self.theme_mode, self.accent_mode, self.semantic_colors_mode)

    def set_appearance_preview(
        self,
        *,
        theme: str | None = None,
        accent: str | None = None,
        title_color: str | None = None,
        density: str | None = None,
        semantic_colors: str | None = None,
    ) -> None:
        """Temporarily preview appearance changes without writing settings.json."""
        if theme is not None:
            mode = str(theme).strip().lower()
            self._preview_theme = mode if mode in THEME_NAMES else None
        if accent is not None:
            self._preview_accent = normalize_accent(accent)
        if title_color is not None:
            self._preview_title_color = normalize_title_color(title_color)
        if density is not None:
            self._preview_density = normalize_density(density)
        if semantic_colors is not None:
            self._preview_semantic_colors = normalize_semantic_colors(semantic_colors)
        self.apply_theme()
        if density is not None:
            self.apply_layout_density()

    def clear_appearance_preview(self, *, apply: bool = True) -> None:
        """Drop any active live preview overrides and optionally repaint."""
        had_preview = (
            self._preview_theme is not None
            or self._preview_accent is not None
            or self._preview_title_color is not None
            or self._preview_density is not None
            or self._preview_semantic_colors is not None
        )
        had_density = self._preview_density is not None
        self._preview_theme = None
        self._preview_accent = None
        self._preview_title_color = None
        self._preview_density = None
        self._preview_semantic_colors = None
        if apply and had_preview:
            self.apply_theme()
            if had_density:
                self.apply_layout_density()

    def apply_theme(self) -> None:
        target = theme_name_for(
            self.theme_mode, self.accent_mode, self.semantic_colors_mode
        )
        if target not in self.available_themes:
            self.register_theme(textual_theme(self.palette))
        self.theme = target
        if not self.is_running:
            return
        from .chrome import Chrome, KeyBar

        for screen in self._all_live_screens():
            refresh_banner = getattr(screen, "refresh_banner", None)
            if callable(refresh_banner):
                try:
                    refresh_banner()
                except Exception:
                    pass
            try:
                for chrome in screen.query(Chrome):
                    chrome.refresh_theme()
                for bar in screen.query(KeyBar):
                    bar.refresh_theme()
            except Exception:
                pass

    def _all_live_screens(self) -> list[object]:
        """Every mounted screen across the active stack and cached home modes."""
        seen: set[int] = set()
        screens: list[object] = []
        stacks: list[list[object]] = [list(getattr(self, "screen_stack", ()))]
        screen_stacks = getattr(self, "_screen_stacks", None)
        if isinstance(screen_stacks, dict):
            for stack in screen_stacks.values():
                if isinstance(stack, list):
                    stacks.append(list(stack))
        for stack in stacks:
            for screen in stack:
                ident = id(screen)
                if ident not in seen:
                    seen.add(ident)
                    screens.append(screen)
        return screens

    def apply_locale(self, *, only_if_changed: bool = False) -> bool:
        """Install the interface translator and redraw labels already on screen."""
        locale = i18n.resolve_locale(self.globals.get("interface_locale", "system"))
        debug = bool(self.globals.get("debug", False))
        current = i18n.get_translator()
        if only_if_changed and current.locale == locale and current.debug == debug:
            return False
        i18n.set_translator(i18n.Translator(locale, debug=debug))
        rtl = i18n.is_rtl_locale(locale)
        self.set_class(rtl, "rtl")
        self._sync_terminal_bidi(enable_explicit=True)
        if not self.is_running:
            return True
        from textual.widgets import OptionList

        for screen in self._all_live_screens():
            # Textual's App.set_class only re-applies CSS to the active top
            # screen; explicitly sync background screens so returning to
            # SettingsScreen or HomeScreen never leaves stale .rtl styles.
            screen.set_class(rtl, "rtl")
            self.stylesheet.update(screen)
            # Flush cached option strips so RTL alignment and Arabic shaping
            # repaint immediately when switching languages live.
            for option_list in screen.query(OptionList):
                cache = getattr(option_list, "_option_render_cache", None)
                if hasattr(cache, "clear"):
                    cache.clear()
                option_list.refresh(layout=True)
            relocalize = getattr(screen, "relocalize", None)
            if callable(relocalize):
                relocalize()
                screen.refresh(layout=True)
                continue
            try:
                from .chrome import Chrome, KeyBar

                for chrome in screen.query(Chrome):
                    chrome.refresh_locale()
                for bar in screen.query(KeyBar):
                    bar.render_pairs()
            except Exception:
                pass
            refresh_keys = getattr(screen, "refresh_keys", None)
            if callable(refresh_keys):
                refresh_keys()
            rebuild = getattr(screen, "rebuild", None)
            if callable(rebuild):
                rebuild()
            refresh = getattr(screen, "refresh_after_settings", None)
            if callable(refresh):
                refresh()
            screen.refresh(layout=True)
        return True

    def next_toggle_theme(self) -> str:
        """Target theme for quick light/dark toggling."""
        current = self.theme_preference
        if palette_for(current).dark:
            return "light"
        fallback = getattr(self, "_last_dark_theme", "dark")
        return fallback if (fallback in THEME_NAMES and palette_for(fallback).dark) else "dark"

    def action_toggle_theme(self) -> None:
        """Toggle between the active dark palette and light palette and remember it."""
        current = self.theme_preference
        if palette_for(current).dark:
            self._last_dark_theme = current
        wanted = self.next_toggle_theme()
        self.globals.set("theme", wanted)
        self.apply_theme()
        self.notify(
            tr("notify.theme", name=tr(f"setting.theme.option.{wanted}", default=wanted)),
            timeout=3,
        )
        refresh = getattr(self.screen, "refresh_after_settings", None)
        if callable(refresh):
            refresh()

    # --------------------------------------------------------------------- cdm
    def apply_device_choice(self) -> None:
        """Push the UI's device choice into the config resolver."""
        self.config.device_override = str(self.globals.get("cdm_device", "") or "")

    def set_device(self, name: str) -> None:
        """Remember a device choice and make it take effect immediately."""
        self.globals.set("cdm_device", name)
        self.apply_device_choice()
        self.invalidate_hints()

    def refresh_resources(self) -> None:
        """Reload CDM/vault configuration after the resource manager saves it.

        The global manager is only reachable from the home settings overlay, so
        no live delivery owns the old Vaults collection here. Rebuilding it gives
        the next service session the new databases/endpoints immediately, while
        mutating the existing global Settings object keeps the settings screen
        that launched the manager in sync with renamed or removed vault names.
        """
        self.config.reload()
        self.config.paths.ensure()
        previous = self.vaults
        try:
            previous.close()
        finally:
            self.vault = KeyVault(self.config.paths.keys_db)
            self.vaults = vaults.build(self.config, local=self.vault)
        refreshed = global_settings(self.settings_store, config=self.config)
        self.globals.replace_specs(refreshed.specs)
        self.apply_device_choice()
        self.invalidate_hints()

    def devices(self) -> list:
        """Every CDM that can be chosen: the files on disk, plus the remote ones.

        Remote CDMs are listed alongside rather than separately because "which CDM
        am I using" is one question - and a remote one is often the only device
        for its system, so hiding it would make that system look unavailable.
        """
        from ..core.cdm import discover
        from ..core.drm import remote_devices

        return remote_devices(self.config.remote_cdms) + discover(self.config.cdm_roots())

    def active_device_system(self) -> str:
        """Which DRM system the selected local or remote device is for."""
        from ..core.cdm import system_of

        name = self.config.device_name_for("")
        remote = self.config.remote_cdm(name)
        if remote is not None:
            return str(remote.system)
        path = self.config.device_for("")
        return system_of(path) if path is not None else ""

    def action_global_quit(self) -> None:
        """Quit, but only on a second press inside the confirm window.

        Escape is bound globally, so a single stray press must not end a
        session that has work in flight. On the delivery screen a first press
        stops the stalled title (and pauses any remaining batch); the second
        press cancels the batch and exits the application.
        """
        now = time.monotonic()
        confirming = (
            self._quit_armed_at is not None
            and now - self._quit_armed_at <= QUIT_CONFIRM_WINDOW
        )
        prepare = getattr(self.screen, "prepare_quit", None)
        custom_notice = False
        if callable(prepare):
            try:
                custom_notice = bool(prepare(second_press=confirming))
            except TypeError:
                prepare()
        if confirming:
            self.exit()
            return
        self._quit_armed_at = now
        if not custom_notice:
            from textual.screen import ModalScreen

            if isinstance(self.screen, ModalScreen):
                self.notify(
                    tr("notify.quit_again_modal", default="Press ^q again to quit"),
                    timeout=QUIT_CONFIRM_WINDOW,
                )
            else:
                self.notify(tr("notify.quit_again"), timeout=QUIT_CONFIRM_WINDOW)

    def disarm_quit(self) -> None:
        """A resumed queue must not confirm an earlier Escape."""
        self._quit_armed_at = None

    def abort_sessions(self) -> None:
        """Tell every live session to stop before the app goes away.

        ``exit()`` only cancels Textual's own workers, and it does so advisorily -
        a download running inside ``Engine.run`` is a synchronous call on a worker
        thread and never notices. So shutdown would wait for it, with every screen
        already gone and nowhere for it to report to.

        The controller registry is authoritative because a worker may still be
        downloading after its screens have been popped.  The screen stack stays
        as a compatibility fallback for headless tests and older screens.
        """
        seen: set[int] = set()
        for controller in list(getattr(self, "_session_controllers", {}).values()):
            if id(controller) in seen:
                continue
            seen.add(id(controller))
            controller.abort()
        for screen in list(self.screen_stack):
            controller = getattr(screen, "controller", None)
            if controller is None or id(controller) in seen:
                continue
            seen.add(id(controller))
            controller.abort()

    def wait_for_sessions(self, timeout: float = SESSION_SHUTDOWN_TIMEOUT) -> bool:
        """Give controller workers one bounded window to finish their cleanup."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        seen: set[int] = set()
        for controller in list(getattr(self, "_session_controllers", {}).values()):
            if id(controller) in seen:
                continue
            seen.add(id(controller))
            wait = getattr(controller, "wait_for_workers", None)
            if not callable(wait):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if not wait(remaining):
                return False
        for screen in list(self.screen_stack):
            controller = getattr(screen, "controller", None)
            if controller is None or id(controller) in seen:
                continue
            seen.add(id(controller))
            wait = getattr(controller, "wait_for_workers", None)
            if not callable(wait):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if not wait(remaining):
                return False
        return True

    def action_global_search(self) -> None:
        """Search is scoped to wherever you are.

        On the main screen it spans services, titles and the whole key vault.
        Inside a service it narrows to that service's keys, because that is the
        question you actually have while you are in there.
        """
        from .search import GlobalSearchScreen

        if isinstance(self.screen, GlobalSearchScreen):
            return
        self.push_screen(GlobalSearchScreen(service=getattr(self.screen, "service", None)))

    _SETTINGS_SCREEN_NAMES = frozenset(
        {
            "SettingsScreen",
            "SettingsGroupScreen",
            "StorageManagerScreen",
            "ProxyManagerScreen",
            "ResourceManagerScreen",
            "ServicesManagerScreen",
            "JustWatchSettingsScreen",
            "CdmScreen",
            "CdmRulesScreen",
        }
    )

    def action_global_settings(self, *, on_dismiss=None) -> None:
        """Settings are scoped the same way.

        Main screen: application-wide behaviour. Inside a service: only that
        service's own options and its track preferences, which apply to it
        alone.
        """
        from .settings_screen import SettingsScreen

        if (
            isinstance(self.screen, (SettingsScreen, ModalScreen))
            or type(self.screen).__name__ in self._SETTINGS_SCREEN_NAMES
            or any(isinstance(screen, SettingsScreen) for screen in self.screen_stack)
        ):
            if callable(on_dismiss):
                on_dismiss()
            return
        service = getattr(self.screen, "service", None)

        def _after(_result) -> None:
            try:
                self.invalidate_hints(getattr(service, "ID", None) if service else None)
                self.apply_theme()  # cheap, and the theme may be what changed
                relocalized = self.apply_locale(only_if_changed=True)
                # a session spans several screens, so refresh all of them, not just
                # the one on top
                controller = getattr(self.screen, "controller", None)
                if controller is not None:
                    controller.refresh_after_settings()
                elif not relocalized:
                    refresh = getattr(self.screen, "refresh_after_settings", None)
                    if callable(refresh):
                        refresh()
                if isinstance(self.screen, HomeScreen):
                    # Textual delivers ScreenResume right after this dismiss callback;
                    # skip its duplicate readiness/export refresh pass.
                    self.screen._skip_resume_refresh = True
                    if not relocalized and getattr(self.screen, "USES_SERVICE_GRID", False):
                        self.screen.rebuild()
            finally:
                if callable(on_dismiss):
                    on_dismiss()

        self.push_screen(SettingsScreen(service, self.globals), _after)

    def action_add_keys(self) -> None:
        """Open the explicit manual-vault writer in the narrowest known scope."""
        from .search import GlobalSearchScreen
        from .vault_screen import AddKeysResult, AddKeysScreen

        if isinstance(self.screen, AddKeysScreen):
            return
        current = self.screen
        service = getattr(current, "service", None)
        service_id = str(getattr(service, "ID", "") or "")
        initial = str(getattr(current, "key_candidate", "") or "")

        def _after(result: AddKeysResult | None) -> None:
            if result is None:
                return
            if isinstance(self.screen, GlobalSearchScreen):
                query = self.screen.query_one("#query").value
                self.screen.rebuild(query)
            severity = "warning" if any(report.error for report in result.reports) else "information"
            self.notify(
                result.summary(),
                title=tr("vault.notify.added_title", service=result.service),
                severity=severity,
                timeout=10,
            )

        self.push_screen(AddKeysScreen(service_id=service_id, initial=initial), _after)


def _exit_deadline():
    """Arm the terminal entry point's last resort for uninterruptible plug-ins."""
    restore_tty = None
    if os.name == "posix" and sys.stdin is not None:
        import termios

        try:
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
        except (OSError, ValueError, termios.error):
            pass
        else:
            def restore_tty():
                termios.tcsetattr(fd, termios.TCSAFLUSH, attrs)

    def force_exit():
        try:
            if restore_tty is not None:
                try:
                    restore_tty()
                except (OSError, ValueError, termios.error):
                    pass
            _restore_terminal_state(clear_input=True)
        finally:
            os._exit(130)

    # Only run() installs this callback. Embedded apps and headless UI tests
    # retain ownership of their process; Back and pause never arm this timer.
    timer = threading.Timer(PROCESS_SHUTDOWN_TIMEOUT, force_exit)
    timer.daemon = True
    lock = threading.Lock()
    armed = False

    def arm():
        nonlocal armed
        # Python can deliver a second Ctrl+C while arm is already running.
        if armed or not lock.acquire(blocking=False):
            return
        try:
            if not armed:
                armed = True
                timer.start()
        finally:
            lock.release()

    return arm, timer.cancel


def run(config_path: Path | None = None, *, config: Config | None = None) -> None:
    from .. import services

    if config is None:
        config = Config.load(config_path)
    # Load service adapters before Textual takes ownership of the terminal.
    # Importing them after application mode starts can queue terminal control
    # sequences while the main UI thread is blocked in module initialization.
    services.load_all(config)
    # iTerm2 reports logical cell points through the generic terminal ioctl on
    # Retina displays.  Prime the native Sixel renderer while stdin is still
    # available so its pixel scaling uses iTerm2's physical cell dimensions.
    from .qr import prime_protocol_image

    prime_protocol_image()
    app = UnidlApp(config)
    # A previous crashed instance may have left these modes enabled before
    # this process started. Reset once before Textual enters application mode so
    # its input parser always starts from a known terminal state.
    _restore_terminal_state()
    app._arm_exit_deadline, cancel_exit_deadline = _exit_deadline()

    def _request_shutdown() -> None:
        # Calling the normal Textual exit path lets on_unmount drain the
        # controller-owned workers while keeping the event loop alive.
        app.exit(return_code=130)

    try:
        with _TerminalStateGuard(_request_shutdown):
            app.run()
    except KeyboardInterrupt:
        # A signal can race with guard installation/teardown, or a service may
        # raise one directly.  Cleanup below is still authoritative; do not
        # print a traceback for an intentional user interrupt.
        pass
    finally:
        try:
            app.abort_sessions()
            if app.wait_for_sessions(SESSION_SHUTDOWN_TIMEOUT):
                cancel_exit_deadline()
        finally:
            # Helpers may restore shared console modes as they exit. Reset
            # after worker cleanup, immediately before returning to the shell.
            _restore_terminal_state(clear_input=True)

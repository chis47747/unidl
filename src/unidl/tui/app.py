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
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from textual.app import App

from ..core import i18n, playready, service_catalog, vaults
from ..core.config import Config, default_config_path
from ..core.credentials import mask_in
from ..core.devreload import ReloadError, ReloadResult, ServiceReloader
from ..core.engine import Engine
from ..core.helpers import HelperResolver
from ..core.i18n import tr
from ..core.secureio import atomic_write_text, private_file
from ..core.service import Service, registry
from ..core.settings import SettingsStore, global_settings
from ..core.vault import KeyVault
from .home import HomeScreen
from .palette import UnidlCommands
from .theme import CSS, THEME_NAMES, THEMES, Palette, palette_for

#: quitting is destructive, so it needs a second press within this window.
QUIT_CONFIRM_WINDOW = 1.5

# Closing sockets and terminating downloader process groups is immediate, but a
# Python worker still needs a moment to unwind its ``finally`` blocks.  Keep the
# whole application wait bounded so an uncooperative service cannot trap exit.
SESSION_SHUTDOWN_TIMEOUT = 5.0

#: Below this many rows the roomy spacing is given up - see the "short screen"
#: block at the end of :mod:`unidl.tui.theme`. Twenty-eight leaves three rows of
#: platforms under the banner with every row of air still in place; below it the
#: air is what has to go, because the list is the screen and the space around it
#: is not.
ROOMY_ROWS = 28

# Textual's Linux driver emits these modes while it owns the terminal.  Its
# normal shutdown path turns them off, but an SSH hangup or a process signal can
# bypass that path and leave iTerm in mouse-reporting mode.  Keep this cleanup
# deliberately independent from Textual so it also covers a renderer failure.
_TERMINAL_RESET = (
    "\x1b[0m"
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?1016l"
    "\x1b[?1004l\x1b[?2004l\x1b[?2026l\x1b[?2048l"
    "\x1b[<u\x1b[?7h\x1b[?47l\x1b[?1047l\x1b[?1049l\x1b[?25h"
)


def _restore_terminal_state(stream=None) -> None:
    """Best-effort reset for terminal modes UniDL/Textual may have enabled."""

    if stream is None:
        stream = getattr(sys, "__stdout__", None) or getattr(sys, "stdout", None)
    if stream is None or getattr(stream, "closed", False):
        return
    try:
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
        atexit.register(_restore_terminal_state)
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
            _restore_terminal_state()

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
        _restore_terminal_state()
        for signum, previous in self._previous.items():
            try:
                signal.signal(signum, previous)
            except (OSError, ValueError):
                pass


class UnidlApp(App):
    CSS = CSS
    TITLE = "unidl"
    COMMANDS = {UnidlCommands}

    BINDINGS = [
        ("ctrl+q", "global_quit", "Quit"),
        # a chord, so it still works with a text field focused
        ("ctrl+t", "toggle_theme", "Theme"),
    ]

    def __init__(self, config: Config | None = None):
        super().__init__()
        self.config = config or Config.load()
        self.config.paths.ensure()
        self.registry = registry
        self._registry_identity = id(self.registry)
        self.settings_store = SettingsStore(self.config.paths.home / "settings.json")
        self.globals = global_settings(self.settings_store, config=self.config)
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
        self._quit_armed_at: float = 0.0
        # A worker can outlive its screens while the native downloader is still
        # active, so shutdown follows controller ownership rather than the UI
        # stack alone.
        self._session_controllers: dict[int, object] = {}
        # a device chosen in a previous run lives in settings.json, not the
        # config file, so it has to be handed to the resolver on the way up
        self.apply_device_choice()
        # Themes have to be registered and selected here, not in on_mount: the
        # stylesheet is parsed before then, and it refers to this theme's own
        # variables, which would be undefined under the default one.
        for theme in THEMES.values():
            self.register_theme(theme)
        self.apply_theme()
        self.apply_locale()

    def register_session(self, controller) -> None:
        self._session_controllers[id(controller)] = controller

    def exit(self, *args, **kwargs):  # noqa: D102 - Textual lifecycle override
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
        self.abort_sessions()
        await asyncio.to_thread(self.wait_for_sessions, SESSION_SHUTDOWN_TIMEOUT)

    def get_default_screen(self) -> HomeScreen:
        # HomeScreen is the bottom of the stack, so pop_screen is always safe
        return HomeScreen()

    def on_resize(self, event) -> None:
        """Trade the spacing for the content when the window is short.

        On the app rather than on each screen: the class is what the stylesheet
        keys off, so setting it here makes every screen - including the ones with
        no resize handler of their own - agree about how much room there is. The
        alternative was five screens each deciding for themselves, which is five
        chances for two of them to disagree while both are on the stack.
        """
        self.set_class(event.size.height < ROOMY_ROWS, "short")

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
    ) -> None:
        from .session import SessionController

        service = self.registry.build(
            service_cls, self.config, self.settings_store, globals_scope=self.globals
        )
        report = service.ctx.helpers
        if not report.ready:
            self.notify(report.blocking_message(), title=tr("notify.needs_setup", name=service_cls.NAME),
                        severity="error", timeout=10)
            return
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
        self.push_screen(controller.start_screen())

    def open_import(self, document) -> bool:
        """Finish what an export file already resolved. Returns whether it started.

        Runs as the service the file came from, deliberately: the naming, the
        settings and the command file all belong to that service, and the file
        would land somewhere else under a stand-in. Nothing is asked of the service
        itself - no sign-in, no catalogue call, no licence - so a service you have
        no account for still imports.
        """
        from .session import SessionController

        service_cls = next(
            (cls for cls in self.registry.all() if cls.ID == document.service), None
        )
        if service_cls is None:
            self.notify(
                f"This export came from '{document.service}', which this build does "
                "not have a service for.",
                title="Cannot import",
                severity="error",
                timeout=10,
            )
            return False
        service = self.registry.build(
            service_cls, self.config, self.settings_store, globals_scope=self.globals
        )
        report = service.ctx.helpers
        if not report.ready:
            # the same gate as opening the service: an import ends in a download,
            # and a download needs the same tools
            self.notify(report.blocking_message(), title=tr("notify.needs_setup", name=service_cls.NAME),
                        severity="error", timeout=10)
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
        self.copy_text(text)
        shown = text.strip().splitlines()[0][:60]
        self.notify(tr("notify.copied", text=shown), timeout=2)

    def copy_text(self, text: str) -> None:
        """Copy through the terminal, and through the OS where we can.

        ``copy_to_clipboard`` uses OSC 52, which several macOS terminals ignore,
        so ``pbcopy`` is used as well when present.
        """
        try:
            self.copy_to_clipboard(text)
        except Exception:
            pass
        tool = shutil.which("pbcopy") or shutil.which("wl-copy") or shutil.which("xclip")
        if not tool:
            return
        argv = [tool] if "xclip" not in tool else [tool, "-selection", "clipboard"]
        try:
            subprocess.run(argv, input=text, text=True, timeout=5, check=False)  # noqa: S603
        except (OSError, subprocess.SubprocessError):
            pass

    def open_url(self, url: str) -> None:
        """Hand a link to the browser, for activation pages you would have to type.

        The same opener as :meth:`open_path`, kept separate because there is
        nothing to say about a URL's ``name`` and because a link that cannot be
        opened has to be readable instead - over SSH the browser that answers is
        not on the screen you are looking at.
        """
        opener = shutil.which("open") or shutil.which("xdg-open")
        if opener is None:
            self.notify(url, title=tr("notify.open_this"), timeout=12)
            return
        try:
            subprocess.Popen([opener, url])  # noqa: S603
            self.notify(tr("notify.opened_browser"), timeout=3)
        except OSError as exc:
            self.notify(f"{url}\n({exc})", title=tr("notify.open_this"), severity="warning", timeout=12)

    # ------------------------------------------------------------------ config
    def open_path(self, path: Path) -> None:
        """Hand a file or folder to whatever the OS opens it with."""
        opener_name = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
        opener = shutil.which(opener_name)
        if opener is None:
            self.notify(str(path), title=tr("notify.path"), timeout=8)
            return
        try:
            subprocess.Popen([opener, str(path)])  # noqa: S603
            self.notify(tr("notify.opened_name", name=path.name), timeout=3)
        except OSError as exc:
            self.notify(tr("notify.open_fail", path=path, error=exc), severity="error", timeout=6)

    def open_config(self) -> None:
        """Open the config file in the system default editor, creating it if needed."""
        path = self.config.source or default_config_path()
        example = Path(__file__).resolve().parents[3] / "unidl.example.yaml"
        try:
            if not path.is_file():
                text = (
                    example.read_text("utf-8")
                    if example.is_file()
                    else "# unidl configuration\n"
                )
                atomic_write_text(path, text)
                self.notify(tr("notify.created", path=path), timeout=4)
            else:
                private_file(path)
            # A second click must open the file just created, not recreate it from
            # the example and erase edits made after the first click.
            self.config.source = path
        except OSError as exc:
            self.notify(tr("notify.create_fail", path=path, error=exc), severity="error", timeout=6)
            return
        self.open_path(path)

    # ------------------------------------------------------------------- theme
    @property
    def theme_preference(self) -> str:
        """The selected palette, normalising legacy ``system`` values to dark."""
        value = str(self.globals.get("theme", "dark") or "dark").lower()
        return value if value in THEME_NAMES else "dark"

    @property
    def theme_mode(self) -> str:
        """The concrete palette currently in use."""
        return self.theme_preference

    @property
    def palette(self) -> Palette:
        """Live colour values, for Rich renderables the stylesheet cannot reach.

        Anything written into the log is a Rich ``Text``, and Rich resolves
        styles itself without knowing about Textual's design tokens, so those
        call sites need the numbers.
        """
        return palette_for(self.theme_mode)

    def apply_theme(self) -> None:
        self.theme = THEME_NAMES.get(self.theme_mode, THEME_NAMES["dark"])

    def apply_locale(self) -> None:
        """Install the interface translator and redraw labels already on screen."""
        locale = i18n.resolve_locale(self.globals.get("interface_locale", "system"))
        i18n.set_translator(
            i18n.Translator(locale, debug=bool(self.globals.get("debug", False)))
        )
        if not self.is_running:
            return
        for screen in list(self.screen_stack):
            relocalize = getattr(screen, "relocalize", None)
            if callable(relocalize):
                relocalize()
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

    def action_toggle_theme(self) -> None:
        """Toggle between the dark and light palettes and remember it."""
        order = ["dark", "light"]
        current = self.theme_preference
        wanted = order[(order.index(current) + 1) % len(order)]
        self.globals.set("theme", wanted)
        self.apply_theme()
        self.notify(tr("notify.theme", name=wanted), timeout=3)
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
        session that has work in flight. On the delivery screen it *does* stop
        that work immediately; the second press only decides whether to close the
        application after the native resume parts have been left on disk.
        """
        prepare = getattr(self.screen, "prepare_quit", None)
        if callable(prepare):
            prepare()
        now = time.monotonic()
        if now - self._quit_armed_at <= QUIT_CONFIRM_WINDOW:
            self.exit()
            return
        self._quit_armed_at = now
        self.notify(tr("notify.quit_again"), timeout=QUIT_CONFIRM_WINDOW)

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

    def action_global_settings(self) -> None:
        """Settings are scoped the same way.

        Main screen: application-wide behaviour. Inside a service: only that
        service's own options and its track preferences, which apply to it
        alone.
        """
        from .settings_screen import SettingsScreen

        service = getattr(self.screen, "service", None)
        if isinstance(self.screen, SettingsScreen):
            return

        def _after(_result) -> None:
            self.invalidate_hints(getattr(service, "ID", None) if service else None)
            self.apply_theme()  # cheap, and the theme may be what changed
            self.apply_locale()
            # a session spans several screens, so refresh all of them, not just
            # the one on top
            controller = getattr(self.screen, "controller", None)
            if controller is not None:
                controller.refresh_after_settings()
            else:
                refresh = getattr(self.screen, "refresh_after_settings", None)
                if callable(refresh):
                    refresh()
            rebuild = getattr(self.screen, "rebuild", None)
            if callable(rebuild) and isinstance(self.screen, HomeScreen):
                rebuild()

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
        initial = current.key_candidate if isinstance(current, GlobalSearchScreen) else ""

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

    def _request_shutdown() -> None:
        app.abort_sessions()
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
        app.abort_sessions()
        app.wait_for_sessions(SESSION_SHUTDOWN_TIMEOUT)

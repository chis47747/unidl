"""Windows Textual driver compatibility shim.

Windows Terminal builds that expose Kitty keyboard protocol support do not all
agree on the mode negotiation used by Textual 8.2.x. The stock driver also
leaves Quick Edit and console mouse input to the host, which can make a painted
TUI stop receiving pointer events. This driver owns those input flags for the
duration of the application and restores them on shutdown.
"""

from __future__ import annotations

import asyncio
import sys
import time
from ctypes import byref, wintypes
from threading import Event, Thread

from textual import constants
from textual._xterm_parser import XTermParser
from textual.drivers import win32
from textual.drivers._writer_thread import WriterThread
from textual.drivers.windows_driver import WindowsDriver

_MOUSE_OFF = (
    "\x1b[?9l\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l\x1b[?1005l"
    "\x1b[?1006l\x1b[?1015l\x1b[?1016l"
)
_MODE_RECOVERY_TIMEOUT = 1.0


class UniDLEventMonitor(win32.EventMonitor):
    """Console reader that preserves printable Unicode from AltGr layouts.

    Textual's stock Windows reader drops any key event that has a control-state
    bit and no virtual-key code.  Windows uses that combination for some
    composed characters, including ``@`` on common non-US layouts.  The
    Unicode character itself is authoritative; only empty modifier records are
    discarded here.
    """

    def run(self) -> None:
        exit_requested = self.exit_event.is_set
        parser = XTermParser(debug=constants.DEBUG)

        try:
            read_count = wintypes.DWORD(0)
            h_in = win32.GetStdHandle(win32.STD_INPUT_HANDLE)
            max_events = 1024
            key_event_type = 0x0001
            window_buffer_size_event = 0x0004
            input_records = (win32.INPUT_RECORD * max_events)()
            read_console_input_w = win32.KERNEL32.ReadConsoleInputW
            keys: list[str] = []

            while not exit_requested():
                for event in parser.tick():
                    self.process_event(event)

                if win32.wait_for_handles([h_in], 100) is None:
                    continue

                read_console_input_w(
                    h_in, byref(input_records), max_events, byref(read_count)
                )
                read_input_records = input_records[: read_count.value]
                keys.clear()
                new_size: tuple[int, int] | None = None

                for input_record in read_input_records:
                    event_type = input_record.EventType
                    if event_type == key_event_type:
                        key_event = input_record.Event.KeyEvent
                        key = key_event.uChar.UnicodeChar
                        if key_event.bKeyDown and key:
                            # Keep printable Unicode from AltGr and non-US
                            # layouts, including ``@``.  Modifier-only and
                            # synthetic control records must not be fed to the
                            # parser: a NUL becomes ``ctrl+@`` and can trigger
                            # the global settings binding for every key.
                            if (
                                key_event.dwControlKeyState
                                and key_event.wVirtualKeyCode == 0
                                and not key.isprintable()
                            ):
                                continue
                            keys.append(key)
                    elif event_type == window_buffer_size_event:
                        size = input_record.Event.WindowBufferSizeEvent.dwSize
                        new_size = (size.X, size.Y)

                if keys:
                    text = "".join(keys).encode("utf-16", "surrogatepass").decode("utf-16")
                    for event in parser.feed(text):
                        self.process_event(event)
                if new_size is not None:
                    self.on_size_change(*new_size)
        except Exception as error:
            self.app.log.error("EVENT MONITOR ERROR", error)


def restore_terminal_state_windows(
    stream, sequence: str, *, clear_input: bool = False
) -> None:
    """Send a final VT reset after restoring the Windows console mode."""
    output_mode = win32.get_console_mode(stream)
    try:
        win32.set_console_mode(
            stream,
            output_mode
            | win32.ENABLE_PROCESSED_OUTPUT
            | win32.ENABLE_VIRTUAL_TERMINAL_PROCESSING,
        )
        stream.write(sequence)
        stream.flush()
    finally:
        win32.set_console_mode(stream, output_mode)

    if clear_input:
        flush_input = win32.KERNEL32.FlushConsoleInputBuffer
        flush_input.argtypes = [wintypes.HANDLE]
        flush_input.restype = wintypes.BOOL
        handle = win32.GetStdHandle(win32.STD_INPUT_HANDLE)
        # ConPTY may forward reports asynchronously. Drain a few times after
        # giving already-in-flight reports a chance to arrive.
        for _ in range(3):
            time.sleep(0.02)
            flush_input(handle)


class UniDLWindowsDriver(WindowsDriver):
    """Windows driver with stable console mouse input and no Kitty negotiation."""

    _KITTY_MODE_SEQUENCES = frozenset({"\x1b[>1u", "\x1b[<u"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Instance-owned: separate driver objects must never share a recovery
        # thread or stop event.
        self._input_mode_stop: Event | None = None
        self._input_mode_thread: Thread | None = None

    def start_application_mode(self) -> None:
        # Keep the stock setup, but install the corrected console reader before
        # it starts.  Calling the base method would create its EventMonitor and
        # reintroduce the printable-AltGr filtering we need to avoid.
        loop = asyncio.get_running_loop()
        self._restore_console = win32.enable_application_mode()
        self._writer_thread = WriterThread(self._file)
        self._writer_thread.start()
        self.write("\x1b[?1049h")
        self._enable_mouse_support()
        self.write("\x1b[?25l")
        self.write("\x1b[?1004h")
        self.write("\x1b[>1u")
        self.flush()
        self._enable_bracketed_paste()
        self._event_thread = UniDLEventMonitor(
            loop, self._app, self.exit_event, self.process_message
        )
        self._event_thread.start()
        expected_mode = win32.get_console_mode(sys.__stdin__)
        if not expected_mode:
            return
        stop = self._input_mode_stop = Event()
        self._input_mode_thread = Thread(
            target=self._maintain_input_mode,
            args=(stop, expected_mode),
            name="unidl-console-mode",
            daemon=True,
        )
        self._input_mode_thread.start()

    def _maintain_input_mode(self, stop: Event, expected_mode: int) -> None:
        while not stop.wait(0.1):
            try:
                if win32.get_console_mode(sys.__stdin__) == expected_mode:
                    continue
                if win32.set_console_mode(sys.__stdin__, expected_mode):
                    self._enable_mouse_support()
            except (OSError, ValueError):
                return

    def _stop_input_mode_recovery(self) -> None:
        stop = self._input_mode_stop
        if stop is None:
            return
        stop.set()
        thread = self._input_mode_thread
        if thread is not None:
            thread.join(_MODE_RECOVERY_TIMEOUT)
        self._input_mode_stop = None
        self._input_mode_thread = None

    def disable_input(self) -> None:
        self._stop_input_mode_recovery()
        super().disable_input()

    def _disable_mouse_support(self) -> None:
        self.write(_MOUSE_OFF)
        self.flush()

    def stop_application_mode(self) -> None:
        super().stop_application_mode()
        # Clear tracking modes again after returning to the primary screen.
        self._disable_mouse_support()

    def close(self) -> None:
        self._stop_input_mode_recovery()
        writer = self._writer_thread
        if writer is not None and writer.is_alive():
            self.disable_input()
            self.write(_MOUSE_OFF)
        super().close()
        try:
            restore_terminal_state_windows(self._file, _MOUSE_OFF, clear_input=True)
        except (OSError, ValueError, AttributeError):
            pass

    def _enable_mouse_support(self) -> None:
        if not self._mouse:
            return
        mode = win32.get_console_mode(sys.__stdin__)
        mode |= win32.ENABLE_MOUSE_INPUT | win32.ENABLE_EXTENDED_FLAGS
        mode &= ~win32.ENABLE_QUICK_EDIT_MODE
        win32.set_console_mode(sys.__stdin__, mode)
        # Use one encoding; legacy urxvt reports can leak numeric ``...M``
        # sequences when the console host changes modes.
        self.write(_MOUSE_OFF)
        self.write("\x1b[?1000h\x1b[?1003h\x1b[?1006h")
        self.flush()

    def write(self, data: str) -> None:
        if data in self._KITTY_MODE_SEQUENCES:
            return
        super().write(data)

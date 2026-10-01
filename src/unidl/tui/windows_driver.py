"""Windows Textual driver compatibility shim.

Windows Terminal builds that expose Kitty keyboard protocol support do not all
agree on the mode negotiation used by Textual 8.2.x.  The stock driver sends
the enable sequence unconditionally, even when its parser is configured not to
consume Kitty key reports.  Suppressing only those two sequences leaves the
ordinary VT mouse, bracketed-paste, resize, and console input paths unchanged.

This module is imported only when :mod:`unidl.tui` selects it on Windows.
"""

from __future__ import annotations

from textual.drivers.windows_driver import WindowsDriver


class UniDLWindowsDriver(WindowsDriver):
    """Windows driver with Kitty keyboard negotiation disabled."""

    _KITTY_MODE_SEQUENCES = frozenset({"\x1b[>1u", "\x1b[<u"})

    def write(self, data: str) -> None:
        if data in self._KITTY_MODE_SEQUENCES:
            return
        super().write(data)

"""Textual front-end. The only package allowed to import a UI framework.

Windows Terminal has two behaviours that are important here: recent builds
enable Kitty keyboard protocol support, and some Windows console paths do not
render Rich true-colour sequences reliably.  Configure those defaults before
Textual is imported so its constants and driver see them during module import.
Users can still override either value explicitly in their environment.
"""

from __future__ import annotations

import os
import sys

from ..core.terminal_cells import install_rich_cell_widths as _install_rich_cell_widths

_install_rich_cell_widths()
del _install_rich_cell_widths

if sys.platform == "win32":
    # Textual's Windows driver can otherwise negotiate Kitty keyboard mode with
    # Windows Terminal.  On some Windows 11 builds that drops ordinary key and
    # mouse reports (notably Ctrl+S), leaving a rendered but inert TUI.
    os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")
    # Textual 8.2.x still enables the Kitty protocol from its Windows driver
    # even when the parser switch above is disabled.  Use UniDL's tiny driver
    # shim to suppress those two mode toggles as well.
    os.environ.setdefault(
        "TEXTUAL_DRIVER", "unidl.tui.windows_driver:UniDLWindowsDriver"
    )
    # Keep the UI colourful without sending true-colour SGR sequences through
    # legacy/conpty paths that may display fragments such as ``38;2;...``.
    os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "256")

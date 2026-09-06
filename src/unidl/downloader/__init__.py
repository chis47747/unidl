"""UniDL's native manifest parser and download engine.

This package is part of the :mod:`unidl` distribution.  Services and the TUI
cross the typed Core delivery boundary; they do not import a second downloader
application or invoke a downloader executable.
"""

from .. import __version__
from .backend import NativeDownloaderBackend, NativeManifestError
from .models import SegmentInfo, StreamInfo
from .parser import parse_source

__all__ = [
    "__version__",
    "NativeDownloaderBackend",
    "NativeManifestError",
    "SegmentInfo",
    "StreamInfo",
    "parse_source",
]

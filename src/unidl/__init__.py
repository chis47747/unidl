"""UniDL - an integrated streaming service browser and download application."""

from .core.binaries import configure_binary_path

__version__ = "2.3.0"

# Keep system installations first while making project-local tools available to
# every existing subprocess lookup and to child processes spawned by the
# downloader.
configure_binary_path()

"""Native UniDL service registry.

Each provider package registers one Service subclass when imported. Keeping the
registry in one module makes service discovery deterministic for the TUI, CLI
and headless tests.
"""

from ..core.service import registry
from . import bbciplayer  # noqa: F401  (registers BBC iPlayer on import)

__all__ = ["bbciplayer"]


def load_all(config=None) -> int:
    """Validate and return the number of services available in this build."""
    registry.validate(config)
    return len(registry.all())

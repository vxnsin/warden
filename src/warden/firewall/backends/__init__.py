"""One module per way of telling a machine what may cross.

Every module in here is imported when a backend is asked for, and every
`Backend` subclass registers itself as it is defined. A new backend is a new
file in this folder and nothing else: there is no list to remember to add it
to, and no central import to forget.
"""

from __future__ import annotations

import pkgutil
from importlib import import_module

_loaded = False


def load() -> None:
    """Import every backend module once, so each has registered itself."""
    global _loaded
    if _loaded:
        return
    for found in pkgutil.iter_modules(__path__):
        if not found.name.startswith("_"):
            import_module(f"{__name__}.{found.name}")
    _loaded = True

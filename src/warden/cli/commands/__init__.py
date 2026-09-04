"""One module per group of commands, all of them found rather than listed.

Every module in this folder is imported when the command line starts, and each
registers its own commands on the shared app as it is defined. Adding a group
is adding a file: there is no list here to remember to add it to.
"""

from __future__ import annotations

import pkgutil
from importlib import import_module

LAST = 999

_loaded = False


def load() -> None:
    """Import every command module, then put the commands in reading order."""
    global _loaded
    if _loaded:
        return

    where: dict[str, int] = {}
    for found in pkgutil.iter_modules(__path__):
        if found.name.startswith("_"):
            continue
        module = import_module(f"{__name__}.{found.name}")
        where[module.__name__] = getattr(module, "ORDER", LAST)

    # Sorted after the fact, because a module cannot say where it belongs until
    # it has been imported - and by then it has already registered. `warden
    # --help` is how somebody reads this tool for the first time, and the order
    # the folder happens to be listed in is not that order.
    from warden.cli.shared import app

    app.registered_commands.sort(
        key=lambda command: where.get(getattr(command.callback, "__module__", ""), LAST)
    )
    _loaded = True

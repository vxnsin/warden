"""What every backend has to be able to do, and what it must never do halfway.

Modelled on `warden.core.autostart`: one class per system. The shape is
deliberately small - a backend translates and applies, it does not decide.
"""

from __future__ import annotations

import platform

from warden.errors import NotPermittedError
from warden.firewall.model import Policy

KNOWN: dict[str, type[Backend]] = {}


class Backend:
    """One system's way of holding a packet filter."""

    kind = "firewall"
    # Which platform.system() values this one serves. Empty means none, which
    # is what the base class wants.
    systems: tuple[str, ...] = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.kind != Backend.kind:
            KNOWN[cls.kind] = cls

    def available(self) -> bool:
        """Whether this machine could use this backend at all."""
        raise NotImplementedError

    def render(self, policy: Policy) -> str:
        """The policy in this system's own words, ready to be read first."""
        raise NotImplementedError

    def apply(self, policy: Policy) -> None:
        """Make it true, in one step or not at all.

        A backend that cannot do this atomically must say so rather than
        applying rule by rule: half a policy is a machine in a state nobody
        asked for, and on a remote host it is a machine nobody can reach.
        """
        raise NotImplementedError

    def snapshot(self) -> str:
        """Everything needed to put this machine back exactly as it is now."""
        raise NotImplementedError

    def restore(self, snapshot: str) -> None:
        raise NotImplementedError


def backend_for(named: str | None = None, system: str | None = None) -> Backend:
    """The backend this machine uses, or the one that was asked for by name."""
    from warden.firewall import backends

    backends.load()
    if named:
        chosen = KNOWN.get(named)
        if chosen is None:
            raise NotPermittedError(
                f"no firewall backend called {named!r} - there is "
                f"{', '.join(sorted(KNOWN)) or 'none at all'}"
            )
        return chosen()

    system = system or platform.system()
    for _, backend in sorted(KNOWN.items()):
        if system in backend.systems:
            return backend()
    raise NotPermittedError(
        f"warden has no firewall backend for {system} yet - "
        f"it knows {', '.join(sorted(KNOWN)) or 'none'}"
    )

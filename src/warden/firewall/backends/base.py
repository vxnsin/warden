"""What every backend has to be able to do, and what it must never do halfway.

Modelled on `warden.autostart`: one class per system, and one function that
picks the right one. The shape is deliberately small - a backend translates and
applies, it does not decide.
"""

from __future__ import annotations

from warden.firewall.model import Policy


class Backend:
    """One system's way of holding a packet filter."""

    kind = "firewall"

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

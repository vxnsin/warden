"""Everything warden will tell you about, and what each one is worth saying.

One list, so the filter, the setup screen, the doctor and the wiki all agree
about what exists. A name here is what somebody writes in `webhook_events`.
"""

from __future__ import annotations

from dataclasses import dataclass

from warden.models import FIREWALL, NODE, PORT


@dataclass(frozen=True)
class Happening:
    scope: str
    action: str
    means: str
    # Whether it goes out by default. A channel told about every heartbeat is a
    # channel people mute within the week.
    notable: bool = True

    @property
    def full(self) -> str:
        return f"{self.scope}.{self.action}"


EVERY = (
    Happening(PORT, "registered", "a name got a port it did not have"),
    Happening(PORT, "renewed", "a heartbeat, or a re-register on the same port", notable=False),
    Happening(PORT, "moved", "a name came back on a different port"),
    Happening(PORT, "released", "given back, by its holder or by hand"),
    Happening(PORT, "expired", "a lease ran out and warden took the port back"),
    Happening(NODE, "joined", "a warden reported in for the first time"),
    Happening(NODE, "returned", "one that had gone quiet is answering again"),
    Happening(NODE, "stale", "one stopped reporting, and is past its lease"),
    Happening(NODE, "forgotten", "one was removed by hand", notable=False),
    Happening(FIREWALL, "applied", "a ruleset was made true on this machine"),
    Happening(FIREWALL, "confirmed", "somebody kept it, and the rollback was called off"),
    Happening(FIREWALL, "rolled_back", "nobody confirmed, so the machine went back"),
    Happening(FIREWALL, "restored", "a snapshot was put back by hand", notable=False),
)

NAMES = tuple(happening.full for happening in EVERY)
NOTABLE = tuple(happening.full for happening in EVERY if happening.notable)

# What each one used to be called, when everything was about ports. Somebody
# has `webhook_events=registered,released` written down already.
BARE = {happening.action: happening.full for happening in EVERY if happening.scope == PORT}


def known(name: str) -> str | None:
    """The full name for whatever somebody wrote, or None if there is no such thing."""
    if name in NAMES:
        return name
    return BARE.get(name)


def wanted(chosen: set[str], event_scope: str, event_action: str) -> bool:
    """Whether this one was asked for, by full name, bare name, or whole scope."""
    full = f"{event_scope}.{event_action}"
    return full in chosen or event_action in chosen or f"{event_scope}.*" in chosen

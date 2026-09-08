"""What a rule is, before any particular firewall has an opinion about it."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from warden.models import Name, Plain


class Direction(StrEnum):
    IN = "in"
    OUT = "out"


class Action(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REJECT = "reject"


class Protocol(StrEnum):
    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"
    ANY = "any"


class Origin(StrEnum):
    """Where a rule came from, which decides what it is allowed to be.

    Kept from the first version rather than added later: a rule written before
    the field existed would have to guess its own history, and the whole point
    of the field is that nothing guesses.
    """

    MANUAL = "manual"
    ADOPTED = "adopted"
    REGISTRY = "registry"
    CATALOGUE = "catalogue"


# An interface name, and nothing that could be read as another word.
Interface = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,31}$", strip_whitespace=True)
]

ANYWHERE = "any"

# How often something may happen: a count, a slash, and a span of time. Kept to
# a shape all three backends that can do this already understand, rather than a
# field somebody can write anything into.
LIMIT = re.compile(r"^([1-9][0-9]{0,5})/(second|minute|hour|day)$")

Limit = Annotated[
    str, StringConstraints(pattern=LIMIT.pattern, strip_whitespace=True)
]

# Seconds in each span, for the backends that count in seconds instead.
SPANS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


# How long something lasts: a number and a unit. `2h`, `30m`, `90s`, `1d`.
SPAN = re.compile(r"^([1-9][0-9]{0,6})(s|m|h|d)$")

LASTS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def span(said: str) -> int:
    """`2h` as seconds. Raises rather than guessing at anything else."""
    found = SPAN.match(said.strip())
    if found is None:
        raise ValueError(
            f"{said!r} is not a length of time - it looks like 30s, 15m, 2h or 1d"
        )
    return int(found[1]) * LASTS[found[2]]


def per_second(limit: str) -> tuple[int, int]:
    """A limit as a count and the seconds it is counted over."""
    said = LIMIT.match(limit)
    if said is None:
        raise ValueError(f"{limit!r} is not a rate")
    return int(said[1]), SPANS[said[2]]


class Rule(BaseModel):
    """One decision about traffic, in terms no firewall backend owns."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    direction: Direction = Direction.IN
    action: Action = Action.ALLOW
    protocol: Protocol = Protocol.TCP
    ports: set[int] = Field(default_factory=set)
    source: str = ANYWHERE
    destination: str = ANYWHERE
    interface: Interface | None = None
    origin: Origin = Origin.MANUAL
    # Where in the ruleset this goes. Lower runs first, because every firewall
    # warden writes for stops at the first rule that matches - so the order is
    # not a presentation detail, it is what the ruleset means.
    priority: int = 100
    # How often this may happen, where the rule lets something through and the
    # backend can say so. `10/second`, `6/minute`.
    limit: Limit | None = None
    # Only ever set for a rule that borrowed a registration's lease.
    service: Name | None = None
    expires_at: datetime | None = None
    comment: Plain | None = None
    enabled: bool = True

    @field_validator("source", "destination")
    @classmethod
    def _an_address_or_anywhere(cls, value: str) -> str:
        if value == ANYWHERE:
            return value
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError:
            raise ValueError(f"{value!r} is not an address or a network") from None

    @field_validator("ports")
    @classmethod
    def _real_ports(cls, value: set[int]) -> set[int]:
        outside = sorted(port for port in value if not 1 <= port <= 65535)
        if outside:
            raise ValueError(f"no such port: {', '.join(str(p) for p in outside)}")
        return value

    @model_validator(mode="after")
    def _ports_belong_to_a_protocol(self) -> Rule:
        if self.ports and self.protocol in (Protocol.ICMP, Protocol.ANY):
            raise ValueError(f"{self.protocol} has no ports to name")
        return self

    @property
    def leased(self) -> bool:
        return self.expires_at is not None

    def expired(self, now: datetime) -> bool:
        """A rule whose service is gone is not a rule any more."""
        return self.expires_at is not None and self.expires_at <= now


class Policy(BaseModel):
    """The default answer, and every rule that argues with it."""

    model_config = ConfigDict(extra="forbid")

    incoming: Action = Action.DENY
    outgoing: Action = Action.ALLOW
    rules: list[Rule] = Field(default_factory=list)

    def live(self, now: datetime) -> list[Rule]:
        """What actually applies, in the order it will be asked.

        Sorted here rather than left to whoever built the list: a backend that
        wrote them in another order would be writing another ruleset.
        """
        return sorted(
            (rule for rule in self.rules if rule.enabled and not rule.expired(now)),
            key=lambda rule: rule.priority,
        )


def runs(ports: set[int]) -> list[tuple[int, int]]:
    """Ports gathered into the stretches they actually form.

    A thousand consecutive ports is one range to a firewall and one range to a
    person; writing it out a thousand times serves neither.
    """
    gathered: list[tuple[int, int]] = []
    for port in sorted(ports):
        if gathered and port == gathered[-1][1] + 1:
            gathered[-1] = (gathered[-1][0], port)
        else:
            gathered.append((port, port))
    return gathered


def spelled(ports: set[int], joiner: str = ",") -> str:
    """Those stretches as text: `8000-8999`, or `80,443`."""
    return joiner.join(
        str(first) if first == last else f"{first}-{last}" for first, last in runs(ports)
    )


def covers(first: Rule, second: Rule) -> bool:
    """Whether everything `second` matches, `first` matches first.

    Every firewall warden writes for stops at the first rule that matches, so a
    rule underneath one that covers it is a rule that never runs. Asked of the
    pair rather than of a packet: this is about the ruleset, not about traffic.
    """
    if first.direction is not second.direction:
        return False
    if first.protocol is not Protocol.ANY and first.protocol is not second.protocol:
        return False
    if first.ports and not second.ports:
        return False
    if first.ports and not second.ports <= first.ports:
        return False
    return _reaches(first.source, second.source) and _reaches(
        first.destination, second.destination
    )


def _reaches(wider: str, narrower: str) -> bool:
    """Whether the first address covers everything the second one does."""
    if wider == ANYWHERE:
        return True
    if narrower == ANYWHERE:
        return False
    try:
        return ipaddress.ip_network(narrower, strict=False).subnet_of(
            ipaddress.ip_network(wider, strict=False)
        )
    except (TypeError, ValueError):
        # One is IPv4 and the other IPv6, so neither reaches the other.
        return False


def shadowed(rules: list[Rule]) -> list[tuple[Rule, Rule]]:
    """Rules that can never run, each with the one standing in front of it.

    In the order they will be applied, so a rule is only ever shadowed by one
    that comes before it.
    """
    found = []
    for index, rule in enumerate(rules):
        earlier = next((one for one in rules[:index] if covers(one, rule)), None)
        if earlier is not None:
            found.append((rule, earlier))
    return found


# What a rule gets when nobody says where it goes, and the room left between
# two of them for one to be slipped in later.
DEFAULT_PRIORITY = 100
STEP = 10


def placed(
    rules: list[Rule], new: Rule, *, before: str | None = None, after: str | None = None
) -> list[Rule]:
    """Where a new rule goes, and every rule that has to move for it.

    Returns what to write, the new rule included. Renumbering rather than
    fractions: the number is a detail of the ordering, and one somebody can
    read in a listing is worth more than one that never has to be rewritten.
    """
    if before is None and after is None:
        highest = max((rule.priority for rule in rules), default=DEFAULT_PRIORITY - STEP)
        return [new.model_copy(update={"priority": highest + STEP})]

    named = before or after
    anchor = next((rule for rule in rules if rule.name == named), None)
    if anchor is None:
        raise ValueError(f"no rule called {named!r} to go {'before' if before else 'after'}")

    wanted = anchor.priority if before else anchor.priority + 1
    moved = [
        rule.model_copy(update={"priority": rule.priority + STEP})
        for rule in rules
        if rule.priority >= wanted and rule.name != new.name
    ]
    return [new.model_copy(update={"priority": wanted}), *moved]

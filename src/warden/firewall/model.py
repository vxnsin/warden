"""What a rule is, before any particular firewall has an opinion about it."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from warden.models import Name


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


ANYWHERE = "any"


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
    interface: str | None = None
    origin: Origin = Origin.MANUAL
    # Only ever set for a rule that borrowed a registration's lease.
    service: str | None = None
    expires_at: datetime | None = None
    comment: str | None = None
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
        """What actually applies: enabled, and not outlived by its service."""
        return [rule for rule in self.rules if rule.enabled and not rule.expired(now)]


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

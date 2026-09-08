"""Named services, so nobody has to remember that sftp is 22 and not 115."""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from warden.errors import WardenError
from warden.firewall.model import ANYWHERE, Action, Direction, Origin, Protocol, Rule

SERVICES: dict[str, tuple[Protocol, set[int]]] = {
    "ssh": (Protocol.TCP, {22}),
    "sftp": (Protocol.TCP, {22}),
    "ftp": (Protocol.TCP, {20, 21}),
    "http": (Protocol.TCP, {80}),
    "https": (Protocol.TCP, {443}),
    "dns": (Protocol.UDP, {53}),
    "dhcp": (Protocol.UDP, {67, 68}),
    "ntp": (Protocol.UDP, {123}),
    "smtp": (Protocol.TCP, {25, 587}),
    "imap": (Protocol.TCP, {143, 993}),
    "rdp": (Protocol.TCP, {3389}),
    "vnc": (Protocol.TCP, {5900}),
    "smb": (Protocol.TCP, {445}),
    "postgres": (Protocol.TCP, {5432}),
    "mysql": (Protocol.TCP, {3306}),
    "redis": (Protocol.TCP, {6379}),
    "mongodb": (Protocol.TCP, {27017}),
    "wireguard": (Protocol.UDP, {51820}),
    "mdns": (Protocol.UDP, {5353}),
}


def look_up(name: str) -> tuple[Protocol, set[int]]:
    """What a name means, or a refusal that helps rather than just refusing."""
    known = SERVICES.get(name.lower())
    if known:
        return known
    near = sorted(other for other in SERVICES if other.startswith(name[:2].lower()))
    hint = f" - did you mean {' or '.join(near)}?" if near else ""
    raise WardenError(f"no service called {name!r}{hint}")


def named(protocol: Protocol, ports: set[int]) -> str | None:
    """The catalogue name for a port, when there is one, for readable output."""
    for name, (its_protocol, its_ports) in SERVICES.items():
        if its_protocol is protocol and its_ports == ports:
            return name
    return None


def describe(protocol: str, ports: object) -> str:
    """What a rule is about, from fields rather than from a parsed rule.

    A hub lists a fleet that may be running a newer warden than itself, so a
    protocol this one has not heard of is printed plainly instead of refusing
    the whole listing.
    """
    from warden.firewall.model import spelled

    if not isinstance(ports, list) or not ports:
        return protocol
    named = None
    with suppress(ValueError):
        named = named_for(protocol, {int(port) for port in ports})
    written = spelled({int(port) for port in ports})
    return f"{protocol}/{written} ({named})" if named else f"{protocol}/{written}"


def named_for(protocol: str, ports: set[int]) -> str | None:
    """The catalogue name for a protocol given by its own name."""
    return named(Protocol(protocol), ports)


PORTS = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")


def numbered(what: str) -> bool:
    """Whether this is a port or a range of them rather than a name."""
    return PORTS.match(what) is not None


def _ports_in(what: str) -> set[int]:
    found = PORTS.match(what)
    first, last = int(found[1]), int(found[2] or found[1])
    if not 1 <= first <= last <= 65535:
        raise ValueError(f"{what!r} is not a port or a range of them")
    return set(range(first, last + 1))


def rule_for(
    what: str,
    *,
    action: Action,
    source: str = ANYWHERE,
    direction: Direction = Direction.IN,
    protocol: str | None = None,
    comment: str | None = None,
    limit: str | None = None,
    for_seconds: int | None = None,
) -> Rule:
    """A port, a port range, or a name out of the catalogue.

    One place, because the command line and the API have to write down the same
    rule for the same words - a rule that means one thing typed and another
    thing asked for is worse than either.
    """
    if numbered(what):
        # The docstring has promised a range since this was written; it just
        # never read one.
        ports = _ports_in(what)
        kind = Protocol(protocol or "tcp")
        origin = Origin.MANUAL
        name = f"{action}-{what}"
    else:
        kind, ports = look_up(what)
        if protocol:
            kind = Protocol(protocol)
        origin = Origin.CATALOGUE
        name = f"{action}-{what.lower()}"
    return Rule(
        name=name,
        direction=direction,
        action=action,
        protocol=kind,
        ports=ports,
        source=source,
        origin=origin,
        comment=comment,
        limit=limit,
        expires_at=(
            datetime.now(UTC) + timedelta(seconds=for_seconds) if for_seconds else None
        ),
    )

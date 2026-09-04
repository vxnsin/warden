"""Named services, so nobody has to remember that sftp is 22 and not 115."""

from __future__ import annotations

from warden.errors import WardenError
from warden.firewall.model import Protocol

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

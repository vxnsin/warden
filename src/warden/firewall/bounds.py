"""What a rule out of the registry is allowed to be.

The registry listens on loopback and asks for no token there. If registering a
service could open the machine to the network, then anything able to register
would be able to open the machine to the network - which is how UPnP became a
byword for this.

So a rule whose origin is the registry passes through here first, and here is
the only place that decides. Two of these carry the weight: the pool bounds
*what* can ever be opened, and the lease bounds *how long*.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime

from warden.config import Settings
from warden.errors import NotPermittedError
from warden.firewall.model import ANYWHERE, Origin, Rule
from warden.models import Registration

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def permitted(rule: Rule, settings: Settings, service: Registration | None = None) -> None:
    """Raise unless the registry may ask for this rule. Silence means yes.

    Only the registry is bounded. A person at the machine writing a rule by
    hand is the authority these bounds exist to protect, not a caller to be
    held at arm's length.
    """
    if rule.origin is not Origin.REGISTRY:
        return

    _switched_on(settings)
    _inside_the_pool(rule, settings)
    _from_a_declared_network(rule, settings)
    if service is not None:
        _not_a_loopback_service(rule, service)
        _no_longer_than_the_lease(rule, service)


def _switched_on(settings: Settings) -> None:
    if not settings.firewall_from_registry:
        raise NotPermittedError(
            "the registry may not open ports here - set firewall_from_registry "
            "and name the networks it may open them to"
        )


def _inside_the_pool(rule: Rule, settings: Settings) -> None:
    """The pool is the boundary, and it is the whole reason this is safe.

    22, 3389 and 445 are outside it, so no registration reaches them - not a
    mistaken one, and not a registry somebody else is driving.
    """
    outside = sorted(
        port for port in rule.ports if not settings.pool_start <= port <= settings.pool_end
    )
    if outside:
        named = ", ".join(str(port) for port in outside)
        raise NotPermittedError(
            f"the registry may only open ports it hands out - {named} is outside "
            f"{settings.pool_start}-{settings.pool_end}"
        )
    if not rule.ports:
        raise NotPermittedError("a rule from the registry has to name the port it is for")


def _from_a_declared_network(rule: Rule, settings: Settings) -> None:
    """Where from is the operator's decision, made once, not the service's."""
    allowed = settings.firewall_allow_from
    if not allowed:
        raise NotPermittedError(
            "no networks are declared in firewall_allow_from, so the registry "
            "has nowhere it may open a port to"
        )
    if rule.source == ANYWHERE:
        raise NotPermittedError(
            "the registry may not open a port to anywhere - it has to name a "
            f"network, one of {', '.join(sorted(allowed))}"
        )
    asked = ipaddress.ip_network(rule.source, strict=False)
    if not any(_within(asked, ipaddress.ip_network(net)) for net in allowed):
        raise NotPermittedError(
            f"{rule.source} is not inside {', '.join(sorted(allowed))}, which is "
            "where the registry may open ports to"
        )


def _within(asked: object, allowed: object) -> bool:
    try:
        return asked.subnet_of(allowed)  # type: ignore[attr-defined]
    except TypeError:
        return False  # one is IPv4 and the other IPv6


def _not_a_loopback_service(rule: Rule, service: Registration) -> None:
    """A service nothing outside can reach has nothing to open."""
    if service.host in LOOPBACK:
        raise NotPermittedError(
            f"{service.name} is bound to {service.host}, which nothing outside this "
            "machine can reach - there is nothing to open"
        )


def _no_longer_than_the_lease(rule: Rule, service: Registration) -> None:
    """A hole that outlives its service is how this idea usually ends."""
    if service.expires_at is None:
        return
    if rule.expires_at is None:
        raise NotPermittedError(
            f"{service.name} holds its port on a lease, so the rule has to expire with it"
        )
    if rule.expires_at > service.expires_at:
        raise NotPermittedError(
            f"the rule would outlive {service.name}'s lease by "
            f"{(rule.expires_at - service.expires_at).total_seconds():.0f}s"
        )


def closed_by(services: list[Registration], rules: list[Rule], now: datetime) -> list[Rule]:
    """Rules whose service is gone, expired or moved elsewhere.

    Called after every sweep of the registry, so a port that goes back to the
    pool does not leave a way in behind it.
    """
    held = {(service.name, service.port) for service in services}
    return [
        rule
        for rule in rules
        if rule.origin is Origin.REGISTRY
        and rule.service is not None
        and (rule.expired(now) or not any((rule.service, port) in held for port in rule.ports))
    ]

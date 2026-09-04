"""Where the registry and the firewall meet, and nowhere else.

Every rule that comes from a registration is built here and checked by
`bounds` before it exists. Keeping that in one place is the point: a second
path that made rules from registrations would be a second path around the
bounds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from warden.core.config import Settings
from warden.errors import UnknownServiceError
from warden.firewall import bounds
from warden.firewall.model import Action, Direction, Origin, Protocol, Rule
from warden.models import Registration

# A window a person opened for themselves, which closes on its own.
DEV_MODE = "dev-mode"


def rule_for(
    service: Registration,
    *,
    source: str,
    settings: Settings,
    protocol: Protocol = Protocol.TCP,
    comment: str | None = None,
) -> Rule:
    """The rule this registration would need, if it is allowed to have one.

    Built and checked together, so there is no moment where an unchecked rule
    from the registry exists at all.
    """
    rule = Rule(
        name=f"allow-{service.name}",
        direction=Direction.IN,
        action=Action.ALLOW,
        protocol=protocol,
        ports={service.port},
        source=source,
        origin=Origin.REGISTRY,
        service=service.name,
        # The lease is the rule's lease. A hole that outlives the service it
        # was opened for is how this idea usually ends.
        expires_at=service.expires_at,
        comment=comment or f"{service.name} ({service.kind})",
    )
    bounds.permitted(rule, settings, service)
    return rule


def found(services: list[Registration], name: str) -> Registration:
    known = next((service for service in services if service.name == name), None)
    if known is None:
        raise UnknownServiceError(f"no service registered as {name!r}")
    return known


def window(settings: Settings, source: str, seconds: int) -> Rule:
    """A development window: the whole pool, from one network, until it lapses.

    Bounded the same way everything from the registry is - it cannot reach a
    port warden does not hand out, and it says when it ends.
    """
    rule = Rule(
        name=DEV_MODE,
        direction=Direction.IN,
        action=Action.ALLOW,
        protocol=Protocol.TCP,
        ports=set(range(settings.pool_start, settings.pool_end + 1)),
        source=source,
        origin=Origin.REGISTRY,
        service=DEV_MODE,
        expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        comment=f"development window, {seconds}s",
    )
    bounds.permitted(rule, settings)
    return rule


def reconcile(
    rules: list[Rule], services: list[Registration], now: datetime | None = None
) -> list[str]:
    """Which rules no longer have a service, and so should not exist.

    The dev-mode window belongs to a person rather than a service, so it is
    left to its own expiry rather than closed for want of a registration.
    """
    moment = now or datetime.now(UTC)
    theirs = [rule for rule in rules if rule.service != DEV_MODE]
    gone = bounds.closed_by(services, theirs, moment)
    lapsed = [
        rule for rule in rules if rule.service == DEV_MODE and rule.expired(moment)
    ]
    return [rule.name for rule in [*gone, *lapsed]]

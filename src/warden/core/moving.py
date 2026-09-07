"""Taking a machine's registrations and rules somewhere else.

The database also holds the history and the snapshots, and neither travels: a
history is a record of what happened on *that* machine, and a snapshot is
another firewall's ruleset, which restoring onto a different machine is the one
thing the whole firewall design exists to make impossible by accident.

So what moves is what a person wrote down: which services hold which ports, and
which rules decide what crosses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from warden import __version__
from warden.core.store import RuleStore, Store
from warden.errors import WardenError
from warden.firewall.model import Rule
from warden.models import Registration

# Bumped when the shape changes in a way an older warden could not read. It has
# not yet, and the field exists so that the first time it does is not the first
# time anybody thinks about it.
SHAPE = 1


@dataclass
class Landing:
    """What an import would do, said before it does it."""

    services: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    # Named rather than counted: a row that will not land is the reason
    # somebody ran this with --dry-run in the first place.
    skipped: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.services) + len(self.rules)


def taken(store: Store) -> dict[str, object]:
    """Everything worth carrying, as something a person can read in a file."""
    return {
        "shape": SHAPE,
        "written_by": __version__,
        "at": datetime.now(UTC).isoformat(),
        "services": [
            service.model_dump(mode="json") for service in store.list()
        ],
        "rules": [rule.model_dump(mode="json") for rule in RuleStore(store).list()],
    }


def read(said: str) -> tuple[list[Registration], list[Rule]]:
    """What is in the file, or a refusal that says what is wrong with it."""
    try:
        body = json.loads(said)
    except ValueError as exc:
        raise WardenError(f"that is not the file warden writes: {exc}") from exc
    if not isinstance(body, dict) or "shape" not in body:
        raise WardenError("that is not the file warden writes - no shape in it")
    if body["shape"] > SHAPE:
        raise WardenError(
            f"the file was written by warden {body.get('written_by', 'later')}, "
            f"whose shape {body['shape']} this one does not know"
        )
    return (
        [Registration.model_validate(one) for one in body.get("services", [])],
        [Rule.model_validate(one) for one in body.get("rules", [])],
    )


def land(
    store: Store,
    services: list[Registration],
    rules: list[Rule],
    *,
    settings,
    keep_ports: bool = True,
    dry_run: bool = False,
) -> Landing:
    """Put them on this machine, and say what happened to each.

    A row at a time rather than all or nothing: a name already taken here, or a
    port outside this machine's pool, is one row's problem and not a reason to
    refuse the other forty.
    """
    said = Landing()
    taken_here = {one.port for one in store.list()}
    known = {one.name for one in store.list()}

    for service in services:
        if service.name in known:
            said.skipped.append(f"{service.name} - already registered here")
            continue
        inside = settings.pool_start <= service.port <= settings.pool_end
        if not inside and keep_ports:
            said.skipped.append(
                f"{service.name} - port {service.port} is outside "
                f"{settings.pool_start}-{settings.pool_end}"
            )
            continue
        if service.port in taken_here:
            said.skipped.append(f"{service.name} - port {service.port} is held here")
            continue
        said.services.append(service.name)
        taken_here.add(service.port)
        known.add(service.name)

    held = {one.name for one in RuleStore(store).list()}
    for rule in rules:
        if rule.name in held:
            said.skipped.append(f"{rule.name} - already written down here")
            continue
        said.rules.append(rule.name)
        held.add(rule.name)

    if dry_run:
        return said

    landing = {one.name for one in services if one.name in said.services}
    store.save_many([one for one in services if one.name in landing])
    arriving = {one.name for one in rules if one.name in said.rules}
    RuleStore(store).save_many([one for one in rules if one.name in arriving])
    return said

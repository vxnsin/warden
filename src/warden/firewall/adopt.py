"""Taking over from whatever is managing packets now.

Two firewalls managing one machine is one firewall too many, so taking over
means reading the other one first and turning it off last. Anything that
cannot be translated is named before anything is applied - a rule quietly
dropped in this step is a door quietly left open, or quietly shut.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

from warden.firewall.model import Action, Direction, Origin, Protocol, Rule

TIMEOUT = 15.0

UFW = "ufw"
FIREWALLD = "firewalld"


@dataclass
class Reading:
    """What another firewall turned out to be holding."""

    manager: str
    rules: list[Rule] = field(default_factory=list)
    # Never silently dropped. A line nobody could translate is a door somebody
    # meant to open or close, and they should hear about it.
    untranslated: list[str] = field(default_factory=list)


def _ask(command: list[str]) -> str | None:
    """Run something that reports, and treat a refusal as "nothing to say"."""
    if shutil.which(command[0]) is None:
        return None
    try:
        finished = subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return finished.stdout if finished.returncode == 0 else None


def managing() -> list[str]:
    """Which firewall managers are running here, in the order they are asked."""
    found = []
    status = _ask(["ufw", "status"])
    if status and "Status: active" in status:
        found.append(UFW)
    state = _ask(["firewall-cmd", "--state"])
    if state and "running" in state:
        found.append(FIREWALLD)
    return found


UFW_ROW = re.compile(
    r"^\[?\s*\d*\]?\s*"
    r"(?P<to>\S+(?: \(v6\))?)\s+"
    r"(?P<action>ALLOW|DENY|REJECT|LIMIT)\s+(?P<way>IN|OUT|FWD)\s+"
    r"(?P<from>.+?)\s*$"
)

VERDICTS = {"ALLOW": Action.ALLOW, "DENY": Action.DENY, "REJECT": Action.REJECT}


def _address(said: str) -> str | None:
    said = said.split("#")[0].strip()
    if said.lower().startswith("anywhere"):
        return "any"
    if "/" in said or said.replace(".", "").isdigit() or ":" in said:
        return said.split(" ")[0]
    return None


def _targets(said: str) -> list[tuple[Protocol, set[int]]] | None:
    """`22/tcp`, `8000:8100/tcp`, `5000-5010/udp`, `3389`, `Anywhere`.

    A bare number means both protocols to ufw, and warden holds one protocol
    per rule, so it becomes two rules rather than half an answer.
    """
    said = said.replace(" (v6)", "").strip()
    if said.lower().startswith("anywhere"):
        return [(Protocol.ANY, set())]

    port, slash, protocol = said.partition("/")
    if slash and protocol not in ("tcp", "udp"):
        return None
    ports = _range(port)
    if ports is None:
        return None
    if not slash:
        return [(Protocol.TCP, ports), (Protocol.UDP, set(ports))]
    return [(Protocol(protocol), ports)]


def _range(said: str) -> set[int] | None:
    """One port, or a stretch of them - ufw writes `a:b`, firewalld `a-b`."""
    first, _, last = said.replace("-", ":").partition(":")
    if not first.isdigit() or (last and not last.isdigit()):
        return None
    return set(range(int(first), int(last) + 1)) if last else {int(first)}

# A line that says ALLOW or DENY is a rule somebody wrote. If it cannot be
# read, it gets named rather than passed over in silence.
LOOKS_LIKE_A_RULE = re.compile(r"\b(ALLOW|DENY|REJECT|LIMIT)\b")


def from_ufw(status: str) -> Reading:
    """`ufw status numbered`, turned into rules warden can hold.

    ufw's own storage is iptables-restore syntax; its status output is the
    thing it documents, so that is what is read.
    """
    reading = Reading(manager=UFW)
    for raw in status.splitlines():
        line = raw.strip()
        if not LOOKS_LIKE_A_RULE.search(line):
            continue
        row = UFW_ROW.match(line)
        if row is None:
            reading.untranslated.append(line)
            continue

        targets = _targets(row["to"])
        source = _address(row["from"])
        action = VERDICTS.get(row["action"])
        if targets is None or source is None or action is None or row["way"] == "FWD":
            reading.untranslated.append(line)
            continue

        for protocol, ports in targets:
            reading.rules.append(
                Rule(
                    name=_named(reading.rules, action, protocol, ports, source),
                    direction=Direction.IN if row["way"] == "IN" else Direction.OUT,
                    action=action,
                    protocol=protocol,
                    ports=ports,
                    source=source,
                    origin=Origin.ADOPTED,
                    comment=f"from ufw: {line}",
                )
            )
    return reading

def _named(
    taken: list[Rule], action: Action, protocol: Protocol, ports: set[int], source: str
) -> str:
    """A name that says what the rule is, and does not collide with its siblings."""
    from warden.firewall.model import spelled

    what = spelled(ports).replace(",", "-") if ports else str(protocol)
    where = "" if source == "any" else "-" + source.split("/")[0].replace(".", "-")
    stem = f"{action}-{what}{where}"[:56]
    if not any(rule.name == stem for rule in taken):
        return stem
    return f"{stem}-{sum(1 for rule in taken if rule.name.startswith(stem)) + 1}"


FIREWALLD_LINE = re.compile(r"^\s*(?P<key>services|ports|sources):\s*(?P<value>.*)$")


def from_firewalld(listing: str, zone_source: str = "any") -> Reading:
    """`firewall-cmd --list-all`, which says what a zone lets through."""
    from warden.firewall.catalogue import SERVICES

    reading = Reading(manager=FIREWALLD)
    sources = []
    entries: dict[str, list[str]] = {"services": [], "ports": []}
    for line in listing.splitlines():
        row = FIREWALLD_LINE.match(line)
        if row is None:
            continue
        said = row["value"].split()
        if row["key"] == "sources":
            sources = said
        else:
            entries[row["key"]] = said

    where = sources[0] if len(sources) == 1 else zone_source
    for name in entries["services"]:
        known = SERVICES.get(name)
        if known is None:
            reading.untranslated.append(f"service {name}")
            continue
        protocol, ports = known
        reading.rules.append(
            Rule(
                name=_named(reading.rules, Action.ALLOW, protocol, ports, where),
                action=Action.ALLOW,
                protocol=protocol,
                ports=set(ports),
                source=where,
                origin=Origin.ADOPTED,
                comment=f"from firewalld: service {name}",
            )
        )
    for entry in entries["ports"]:
        targets = _targets(entry)
        if targets is None:
            reading.untranslated.append(f"port {entry}")
            continue
        for protocol, ports in targets:
            reading.rules.append(
                Rule(
                    name=_named(reading.rules, Action.ALLOW, protocol, ports, where),
                    action=Action.ALLOW,
                    protocol=protocol,
                    ports=ports,
                    source=where,
                    origin=Origin.ADOPTED,
                    comment=f"from firewalld: port {entry}",
                )
            )
    return reading


def read(manager: str) -> Reading:
    """Ask a running firewall what it is holding."""
    if manager == UFW:
        return from_ufw(_ask(["ufw", "status", "numbered"]) or "")
    if manager == FIREWALLD:
        return from_firewalld(_ask(["firewall-cmd", "--list-all"]) or "")
    return Reading(manager=manager)


def stand_down(manager: str) -> list[str]:
    """Turn the other firewall off. Only ever called after the switch."""
    steps = {
        UFW: [["ufw", "--force", "disable"]],
        FIREWALLD: [
            ["systemctl", "stop", "firewalld"],
            ["systemctl", "disable", "firewalld"],
        ],
    }.get(manager, [])
    done = []
    for step in steps:
        _ask(step)
        done.append(" ".join(step))
    return done

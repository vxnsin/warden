"""Reading a hand-written nftables ruleset, and saying what could not be read.

`nft -j list ruleset` is JSON, which is the reason nftables is the one warden
learns to read first. Most of what a hand-written ruleset does - chains, jumps,
connection tracking, NAT, marks, sets - warden has no word for, and every line
of it is named rather than dropped: what somebody reads before deciding to let
warden take over is this report, not the rules that made it through.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

from warden.firewall.model import Action, Direction, Protocol

# Which end of a rule an address belongs to, and which way traffic goes.
HOOKS = {"input": Direction.IN, "output": Direction.OUT}

VERDICTS = {"accept": Action.ALLOW, "drop": Action.DENY, "reject": Action.REJECT}

# Bookkeeping that changes nothing about what crosses. A counter on a rule is
# not a decision, so it does not stop the rule being read.
IGNORED = ("counter",)

PORTS = ("dport",)
ADDRESSES = {"saddr": "source", "daddr": "destination"}

# `# handle 4` at the end of a line, which is how the words a person wrote are
# paired with the JSON that was parsed.
HANDLE = re.compile(r"#\s*handle\s+(\d+)\s*$")


class UnreadableError(Exception):
    """Something in the rule warden has no word for."""


def rules_in(said: str) -> int:
    """How many rules a `nft -j list ruleset` answer holds, without reading them."""
    try:
        found = json.loads(said)
    except (TypeError, ValueError):
        return 0
    return sum(1 for entry in found.get("nftables", []) if "rule" in entry)


def worded(listing: str) -> dict[int, str]:
    """What `nft -a list ruleset` printed, by handle - a person's own words."""
    found = {}
    for line in listing.splitlines():
        said = HANDLE.search(line)
        if said is not None:
            found[int(said[1])] = line.strip()
    return found


def _chains(entries: list[dict]) -> dict[tuple[str, str, str], dict]:
    return {
        (one["chain"]["family"], one["chain"]["table"], one["chain"]["name"]): one["chain"]
        for one in entries
        if "chain" in one
    }


def read(said: str, listing: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """Every rule as the fields warden holds, and every line it could not read.

    The rules come back as plain dictionaries rather than `Rule`s: naming them
    is `adopt`'s job, and it names them against everything it has taken so far.
    """
    try:
        found = json.loads(said)
    except (TypeError, ValueError):
        return [], ["nft did not answer with json"]

    entries = found.get("nftables", [])
    chains = _chains(entries)
    words = worded(listing)

    taken: list[dict[str, Any]] = []
    lost: list[str] = []
    for entry in entries:
        rule = entry.get("rule")
        if rule is None:
            continue
        try:
            taken.append(_one(rule, chains))
        except UnreadableError as why:
            lost.append(_said(rule, words, str(why)))
    return taken, lost


def _said(rule: dict, words: dict[int, str], why: str) -> str:
    """The line in the words it was written in, and what stopped it being read."""
    handle = rule.get("handle")
    written = words.get(handle) if isinstance(handle, int) else None
    if written is None:
        written = f"{rule.get('chain', '?')} handle {handle}"
    return f"{written}  ({why})"


def _one(rule: dict, chains: dict) -> dict[str, Any]:
    where = chains.get((rule.get("family"), rule.get("table"), rule.get("chain")))
    if where is None or "hook" not in where:
        # A rule in a chain nothing hooks is only reached by a jump, and a jump
        # is not something warden can hold either.
        raise UnreadableError("in a chain that is only reached by a jump")
    if where.get("type") != "filter":
        raise UnreadableError(f"a {where.get('type')} chain, not a filter")
    direction = HOOKS.get(where["hook"])
    if direction is None:
        raise UnreadableError(f"hooks {where['hook']}, which warden does not hold")

    said: dict[str, Any] = {"direction": direction}
    action = None
    for expression in rule.get("expr", []):
        if not isinstance(expression, dict) or len(expression) != 1:
            raise UnreadableError("an expression warden cannot read")
        (kind, body), = expression.items()
        if kind in IGNORED:
            continue
        if kind in VERDICTS:
            action = VERDICTS[kind]
            continue
        if kind == "match":
            _match(body, said)
            continue
        if kind == "limit":
            said["limit"] = _limit(body)
            continue
        raise UnreadableError(f"{kind}, which warden has no word for")

    if action is None:
        raise UnreadableError("no verdict, so it only counts or logs")
    said["action"] = action
    said.setdefault("protocol", Protocol.ANY if not said.get("ports") else Protocol.TCP)
    if said.get("ports") and said["protocol"] is Protocol.ANY:
        raise UnreadableError("names ports without saying which protocol")
    return said


def _match(body: dict, said: dict[str, Any]) -> None:
    if body.get("op") not in ("==", "in"):
        raise UnreadableError(f"matches with {body.get('op')!r}")
    left, right = body.get("left"), body.get("right")
    if not isinstance(left, dict) or len(left) != 1:
        raise UnreadableError("a match warden cannot read")
    (kind, what), = left.items()

    if kind == "meta" and what.get("key") in ("iifname", "oifname"):
        if not isinstance(right, str):
            raise UnreadableError("names more than one interface")
        said["interface"] = right
        return
    if kind == "meta" and what.get("key") == "l4proto":
        said["protocol"] = _protocol(right)
        return
    if kind == "payload":
        _payload(what, right, said)
        return
    raise UnreadableError(f"matches on {kind}, which warden has no word for")


def _payload(what: dict, right: object, said: dict[str, Any]) -> None:
    field = what.get("field")
    if field in PORTS:
        said["protocol"] = _protocol(what.get("protocol"))
        said["ports"] = _ports(right)
        return
    if field in ADDRESSES:
        said[ADDRESSES[field]] = _address(right)
        return
    if field == "protocol":
        said["protocol"] = _protocol(right)
        return
    raise UnreadableError(f"matches on {field}, which warden has no word for")


def _protocol(said: object) -> Protocol:
    try:
        return Protocol(str(said))
    except ValueError:
        raise UnreadableError(f"is about {said}, which warden has no word for") from None


def _ports(right: object) -> set[int]:
    if isinstance(right, int):
        return {right}
    if isinstance(right, dict) and "range" in right:
        first, last = right["range"]
        return set(range(int(first), int(last) + 1))
    if isinstance(right, dict) and "set" in right:
        ports: set[int] = set()
        for one in right["set"]:
            ports |= _ports(one)
        return ports
    raise UnreadableError("names ports warden cannot read")


def _address(right: object) -> str:
    if isinstance(right, str):
        try:
            ipaddress.ip_network(right, strict=False)
        except ValueError:
            raise UnreadableError(f"is about {right!r}, which is not an address") from None
        return right
    if isinstance(right, dict) and "prefix" in right:
        prefix = right["prefix"]
        return f"{prefix['addr']}/{prefix['len']}"
    if isinstance(right, dict) and "set" in right:
        raise UnreadableError("names a set of addresses, and warden holds one a rule")
    raise UnreadableError("names an address warden cannot read")


def _limit(body: dict) -> str:
    if body.get("inv"):
        raise UnreadableError("limits what is over the rate rather than under it")
    rate, per = body.get("rate"), body.get("per")
    if not isinstance(rate, int) or per not in ("second", "minute", "hour", "day"):
        raise UnreadableError("has a rate warden cannot write")
    if body.get("rate_unit", "packets") != "packets":
        raise UnreadableError("limits bytes rather than packets")
    return f"{rate}/{per}"

"""nftables, which is the only one of these with a real transaction.

A whole ruleset in one file, applied with `nft -f`, replaces everything or
nothing. That is what makes changing a firewall on a machine you are logged
into over the network something other than a gamble.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

from warden.firewall.backends.base import Backend
from warden.firewall.model import Action, Direction, Policy, Protocol, Rule

TABLE = "warden"

VERDICTS = {Action.ALLOW: "accept", Action.DENY: "drop", Action.REJECT: "reject"}

HEADER = "# Written by warden. Regenerate it; do not edit it."


def _ports(rule: Rule) -> str:
    if not rule.ports:
        return ""
    ordered = sorted(rule.ports)
    named = str(ordered[0]) if len(ordered) == 1 else "{ " + ", ".join(map(str, ordered)) + " }"
    return f" {rule.protocol} dport {named}"


def _addresses(rule: Rule) -> str:
    parts = []
    if rule.source != "any":
        parts.append(f" {_family(rule.source)} saddr {rule.source}")
    if rule.destination != "any":
        parts.append(f" {_family(rule.destination)} daddr {rule.destination}")
    return "".join(parts)


def _family(network: str) -> str:
    return "ip6" if ":" in network else "ip"


def _comment(rule: Rule) -> str:
    said = rule.comment or rule.name
    return f' comment "{said}"' if said else ""


def line(rule: Rule) -> str:
    """One rule, as nftables would have written it."""
    parts = ""
    if rule.interface:
        parts += f" {'iif' if rule.direction is Direction.IN else 'oif'} {rule.interface}"
    if rule.protocol is Protocol.ICMP:
        parts += " ip protocol icmp"
    parts += _addresses(rule) + _ports(rule)
    return f"{parts.strip()} {VERDICTS[rule.action]}{_comment(rule)}".strip()


class Nftables(Backend):
    kind = "nftables"

    def available(self) -> bool:
        return shutil.which("nft") is not None

    def render(self, policy: Policy, now: datetime | None = None) -> str:
        live = policy.live(now or datetime.now(UTC))
        incoming = [rule for rule in live if rule.direction is Direction.IN]
        outgoing = [rule for rule in live if rule.direction is Direction.OUT]

        lines = [
            "#!/usr/sbin/nft -f",
            HEADER,
            "",
            "flush ruleset",
            "",
            f"table inet {TABLE} {{",
            "\tchain input {",
            f"\t\ttype filter hook input priority filter; policy {VERDICTS[policy.incoming]};",
            "",
            "\t\t# Answers to things this machine asked for. Without this line a",
            "\t\t# default-drop policy also drops the session applying it.",
            "\t\tct state established,related accept",
            "\t\tct state invalid drop",
            "\t\tiif lo accept",
            "",
        ]
        lines += [f"\t\t{line(rule)}" for rule in incoming]
        lines += [
            "\t}",
            "",
            "\tchain forward {",
            "\t\ttype filter hook forward priority filter; policy drop;",
            "\t}",
            "",
            "\tchain output {",
            f"\t\ttype filter hook output priority filter; policy {VERDICTS[policy.outgoing]};",
            "\t\tct state established,related accept",
            "\t\toif lo accept",
        ]
        lines += [f"\t\t{line(rule)}" for rule in outgoing]
        lines += ["\t}", "}", ""]
        return "\n".join(lines)

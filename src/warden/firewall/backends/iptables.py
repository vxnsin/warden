"""iptables, for the Linux machines that have not moved to nftables.

`iptables-restore` takes a whole table and swaps it in one netlink
transaction, which is the same promise nft makes and the reason this can be
done safely at all.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime

from warden.errors import FirewallError, NotPermittedError
from warden.firewall.backends.base import Backend
from warden.firewall.model import (
    Action,
    Direction,
    Policy,
    Protocol,
    Rule,
    per_second,
    spelled,
)

TIMEOUT = 20.0

VERDICTS = {Action.ALLOW: "ACCEPT", Action.DENY: "DROP", Action.REJECT: "REJECT"}

CHAINS = {Direction.IN: "INPUT", Direction.OUT: "OUTPUT"}


# What iptables calls each span. It has no word for a day.
UNITS = {1: "second", 60: "minute", 3600: "hour", 86400: "day"}


def line(rule: Rule) -> str:
    """One rule, as iptables-restore would have written it."""
    for said in (rule.source, rule.destination):
        if ":" in said:
            # `iptables-restore` is IPv4 and would refuse the whole table, which
            # means every other rule with it. Better to name the one rule than
            # to hand over a file that cannot load.
            raise NotPermittedError(
                f"{rule.name} names {said}, and iptables is IPv4 only - "
                "nftables holds both families in one ruleset"
            )
    parts = [f"-A {CHAINS[rule.direction]}"]
    if rule.interface:
        parts.append(f"{'-i' if rule.direction is Direction.IN else '-o'} {rule.interface}")
    if rule.protocol is not Protocol.ANY:
        parts.append(f"-p {rule.protocol}")
    if rule.source != "any":
        parts.append(f"-s {rule.source}")
    if rule.destination != "any":
        parts.append(f"-d {rule.destination}")
    if rule.ports:
        spoken = spelled(rule.ports, ",").replace("-", ":")
        many = len(rule.ports) > 1 or "-" in spelled(rule.ports)
        parts.append(f"-m multiport --dports {spoken}" if many else f"--dport {spoken}")
    if rule.limit:
        count, over = per_second(rule.limit)
        parts.append(f"-m limit --limit {count}/{UNITS[over]}")
    said = (rule.comment or rule.name).replace('"', "'")[:255]
    parts.append(f'-m comment --comment "{said}"')
    parts.append(f"-j {VERDICTS[rule.action]}")
    return " ".join(parts)


class Iptables(Backend):
    kind = "iptables"
    systems = ()  # asked for by name; nftables is what a modern Linux gets

    def available(self) -> bool:
        return shutil.which("iptables-restore") is not None

    def render(self, policy: Policy, now: datetime | None = None) -> str:
        live = policy.live(now or datetime.now(UTC))
        lines = [
            "# Written by warden. Regenerate it; do not edit it.",
            "*filter",
            f":INPUT {VERDICTS[policy.incoming]} [0:0]",
            ":FORWARD DROP [0:0]",
            f":OUTPUT {VERDICTS[policy.outgoing]} [0:0]",
            "# Answers to things this machine asked for. Without this line a",
            "# default-drop policy also drops the session applying it.",
            "-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            "-A INPUT -i lo -j ACCEPT",
            "-A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            "-A OUTPUT -o lo -j ACCEPT",
        ]
        lines += [line(rule) for rule in live]
        lines += ["COMMIT", ""]
        return "\n".join(lines)

    def apply(self, policy: Policy) -> None:
        self._run(["iptables-restore"], self.render(policy), "applying the ruleset")

    def snapshot(self) -> str:
        return self._run(["iptables-save"], None, "reading the ruleset")

    def restore(self, snapshot: str) -> None:
        self._run(["iptables-restore"], snapshot, "restoring the ruleset")

    def _run(self, command: list[str], stdin: str | None, doing: str) -> str:
        if not self.available():
            raise FirewallError("no iptables on this machine, so there is nothing to talk to")
        try:
            finished = subprocess.run(
                command, input=stdin, capture_output=True, text=True,
                timeout=TIMEOUT, check=False,
            )
        except OSError as exc:
            raise FirewallError(f"could not run {command[0]} - {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FirewallError(f"{command[0]} took longer than {TIMEOUT:g}s {doing}") from exc
        if finished.returncode != 0:
            said = (finished.stderr or finished.stdout).strip() or f"exit {finished.returncode}"
            if "Permission denied" in said or "must be root" in said:
                raise NotPermittedError(f"{command[0]} refused: a firewall needs root")
            raise FirewallError(f"{command[0]} failed {doing}: {said.splitlines()[0]}")
        return finished.stdout

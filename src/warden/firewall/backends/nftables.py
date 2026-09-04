"""nftables, which is the only one of these with a real transaction.

A whole ruleset in one file, applied with `nft -f`, replaces everything or
nothing. That is what makes changing a firewall on a machine you are logged
into over the network something other than a gamble.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime

from warden.errors import FirewallError, NotPermittedError
from warden.firewall.backends.base import Backend
from warden.firewall.model import Action, Direction, Policy, Protocol, Rule

TABLE = "warden"

VERDICTS = {Action.ALLOW: "accept", Action.DENY: "drop", Action.REJECT: "reject"}

HEADER = "# Written by warden. Regenerate it; do not edit it."

# A firewall change that hangs is worse than one that fails.
TIMEOUT = 20.0


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
    systems = ("Linux",)

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
    def apply(self, policy: Policy) -> None:
        self.load(self.render(policy))

    def snapshot(self) -> str:
        """Everything currently loaded, in a form `nft -f` will take back."""
        return self._nft(["list", "ruleset"], doing="reading the ruleset")

    def restore(self, snapshot: str) -> None:
        """Put back exactly what was there, including an empty ruleset."""
        self.load("flush ruleset\n" + snapshot)

    def load(self, ruleset: str) -> None:
        """One file, one transaction. Either all of it applies or none does.

        This is the whole reason nftables came first. Applying rule by rule
        would leave a machine half-governed, and on a remote host that half is
        where the ssh session used to be.
        """
        self._nft(["-f", "-"], stdin=ruleset, doing="applying the ruleset")

    def _nft(self, arguments: list[str], *, stdin: str | None = None, doing: str) -> str:
        if not self.available():
            raise FirewallError("no nft on this machine, so there is nothing to talk to")
        try:
            finished = subprocess.run(
                ["nft", *arguments],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                check=False,
            )
        except OSError as exc:
            raise FirewallError(f"could not run nft - {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FirewallError(f"nft took longer than {TIMEOUT:g}s {doing}") from exc

        if finished.returncode != 0:
            said = (finished.stderr or finished.stdout).strip() or f"exit {finished.returncode}"
            if "Operation not permitted" in said or "must be root" in said:
                raise NotPermittedError(
                    f"nft refused: {said.splitlines()[0]} - a firewall needs root"
                )
            raise FirewallError(f"nft failed {doing}: {said.splitlines()[0]}")
        return finished.stdout

"""pf, which macOS and the BSDs use.

`pfctl -f` loads a whole ruleset at once, so the same all-or-nothing promise
holds here as it does for nftables.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime

from warden.errors import FirewallError, NotPermittedError
from warden.firewall.backends.base import Backend
from warden.firewall.model import Action, Direction, Policy, Protocol, Rule, runs, spelled

TIMEOUT = 20.0

VERDICTS = {Action.ALLOW: "pass", Action.DENY: "block drop", Action.REJECT: "block return"}


def _ports(rule: Rule) -> str:
    """pf writes a range `a:b` and a list `{ a, b }`, and minds the difference."""
    spoken = spelled(rule.ports, ", ").replace("-", ":")
    return spoken if len(runs(rule.ports)) == 1 else "{ " + spoken + " }"


def line(rule: Rule) -> str:
    """One rule, as pf.conf would have written it."""
    parts = [VERDICTS[rule.action], "in" if rule.direction is Direction.IN else "out"]
    if rule.interface:
        parts.append(f"on {rule.interface}")
    if rule.protocol is not Protocol.ANY:
        parts.append(f"proto {rule.protocol}")
    parts.append("from " + (rule.source if rule.source != "any" else "any"))
    if rule.ports and rule.direction is Direction.OUT:
        parts.append("to any port " + _ports(rule))
    else:
        parts.append("to " + (rule.destination if rule.destination != "any" else "any"))
        if rule.ports:
            parts.append("port " + _ports(rule))
    if rule.action is Action.ALLOW and rule.direction is Direction.IN:
        # Without this, the answer to an accepted connection has nowhere to go.
        parts.append("keep state")
    said = (rule.comment or rule.name).replace('"', "'")
    return " ".join(parts) + f'  # {said}'


class Pf(Backend):
    kind = "pf"
    systems = ("Darwin", "FreeBSD", "OpenBSD", "NetBSD")

    def available(self) -> bool:
        return shutil.which("pfctl") is not None

    def render(self, policy: Policy, now: datetime | None = None) -> str:
        live = policy.live(now or datetime.now(UTC))
        lines = [
            "# Written by warden. Regenerate it; do not edit it.",
            "set skip on lo0",
            "",
            f"block {'drop' if policy.incoming is Action.DENY else 'return'} in all"
            if policy.incoming is not Action.ALLOW
            else "pass in all",
            "pass out all keep state" if policy.outgoing is Action.ALLOW else "block out all",
            "",
        ]
        lines += [line(rule) for rule in live]
        lines += [""]
        return "\n".join(lines)

    def apply(self, policy: Policy) -> None:
        self._run(["pfctl", "-f", "-"], self.render(policy), "applying the ruleset")

    def snapshot(self) -> str:
        return self._run(["pfctl", "-sr"], None, "reading the ruleset")

    def restore(self, snapshot: str) -> None:
        self._run(["pfctl", "-f", "-"], snapshot, "restoring the ruleset")

    def _run(self, command: list[str], stdin: str | None, doing: str) -> str:
        if not self.available():
            raise FirewallError("no pfctl on this machine, so there is nothing to talk to")
        try:
            finished = subprocess.run(
                command, input=stdin, capture_output=True, text=True,
                timeout=TIMEOUT, check=False,
            )
        except OSError as exc:
            raise FirewallError(f"could not run pfctl - {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FirewallError(f"pfctl took longer than {TIMEOUT:g}s {doing}") from exc
        if finished.returncode != 0:
            said = (finished.stderr or finished.stdout).strip() or f"exit {finished.returncode}"
            if "Operation not permitted" in said or "Permission denied" in said:
                raise NotPermittedError("pfctl refused: a firewall needs root")
            raise FirewallError(f"pfctl failed {doing}: {said.splitlines()[0]}")
        return finished.stdout

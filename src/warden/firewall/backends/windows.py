"""Windows Defender Firewall, through netsh.

Windows Defender Firewall filters statefully whichever way it is told, so
unlike the others there is no established-traffic rule to write: allowing
outbound is what lets the answers back in.

It is also the one backend with no transaction of its own - netsh adds rules
one at a time. So this takes its own snapshot first and puts it back if anything fails
partway, which is the same promise made by other means.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from warden.errors import FirewallError, NotPermittedError
from warden.firewall.backends.base import Backend
from warden.firewall.model import Action, Direction, Policy, Protocol, Rule, spelled

TIMEOUT = 30.0

# Every rule warden writes carries this, so warden's rules can be removed as a
# set without touching anything anybody else put there.
GROUP = "warden"

VERDICTS = {Action.ALLOW: "allow", Action.DENY: "block", Action.REJECT: "block"}


def line(rule: Rule) -> str:
    """One rule, as netsh would have been told it."""
    parts = [
        "netsh advfirewall firewall add rule",
        f'name="{GROUP}: {rule.name}"',
        f'group="{GROUP}"',
        f"dir={'in' if rule.direction is Direction.IN else 'out'}",
        f"action={VERDICTS[rule.action]}",
    ]
    if rule.protocol is not Protocol.ANY:
        parts.append(f"protocol={rule.protocol}")
    if rule.ports:
        parts.append(f"localport={spelled(rule.ports)}")
    if rule.source != "any":
        parts.append(f"remoteip={rule.source}")
    said = (rule.comment or rule.name).replace('"', "'")
    parts.append(f'description="{said}"')
    return " ".join(parts)


class Windows(Backend):
    kind = "windows"
    systems = ("Windows",)

    def available(self) -> bool:
        return shutil.which("netsh") is not None

    def render(self, policy: Policy, now: datetime | None = None) -> str:
        live = policy.live(now or datetime.now(UTC))
        lines = [
            ":: Written by warden. Regenerate it; do not edit it.",
            f'netsh advfirewall firewall delete rule group="{GROUP}"',
            "netsh advfirewall set allprofiles firewallpolicy "
            f"{'blockinbound' if policy.incoming is not Action.ALLOW else 'allowinbound'}"
            f",{'allowoutbound' if policy.outgoing is Action.ALLOW else 'blockoutbound'}",
        ]
        lines += [line(rule) for rule in live]
        lines += [""]
        return "\n".join(lines)

    def apply(self, policy: Policy) -> None:
        """Add them one by one, and put everything back if one of them fails.

        netsh has no transaction, so the promise is kept with a snapshot rather
        than by the system: a half-applied policy is never left standing.
        """
        before = self.snapshot()
        try:
            for command in self.render(policy).splitlines():
                if command.startswith("::") or not command.strip():
                    continue
                self._run(command.split(), "applying a rule", allow_fail=command.count("delete"))
        except Exception:
            self.restore(before)
            raise

    def snapshot(self) -> str:
        """A policy file, which is what Windows can actually give back."""
        # A fresh, unguessable path rather than a known one: this file is
        # imported back as firewall policy, so anything able to write it first
        # would be choosing the rules.
        handle, path = tempfile.mkstemp(prefix="warden-firewall-", suffix=".wfw")
        os.close(handle)
        where = Path(path)
        where.unlink(missing_ok=True)  # netsh writes it itself, and wants it gone
        self._run(
            ["netsh", "advfirewall", "export", str(where)], "reading the current policy"
        )
        return str(where)

    def restore(self, snapshot: str) -> None:
        if not Path(snapshot).is_file():
            raise FirewallError(f"the saved policy at {snapshot} is not there any more")
        self._run(["netsh", "advfirewall", "import", snapshot], "restoring the policy")

    def _run(self, command: list[str], doing: str, allow_fail: bool = False) -> str:
        if not self.available():
            raise FirewallError("no netsh on this machine, so there is nothing to talk to")
        try:
            finished = subprocess.run(
                command, capture_output=True, text=True, timeout=TIMEOUT, check=False
            )
        except OSError as exc:
            raise FirewallError(f"could not run netsh - {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FirewallError(f"netsh took longer than {TIMEOUT:g}s {doing}") from exc
        if finished.returncode != 0 and not allow_fail:
            said = (finished.stdout or finished.stderr).strip() or f"exit {finished.returncode}"
            first = said.splitlines()[0]
            if "elevation" in said.lower() or "Zugriff" in said or "denied" in said.lower():
                raise NotPermittedError(
                    "netsh refused: a firewall needs an elevated prompt on Windows"
                )
            raise FirewallError(f"netsh failed {doing}: {first}")
        return finished.stdout

"""Applying a firewall in a way that a mistake does not end the conversation.

Every apply takes a snapshot first and arms a rollback. If nobody confirms
within the window, the machine goes back to what it had. That window is the
difference between a wrong rule and a server you have to drive to.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from warden.core.store import Snapshots
from warden.errors import FirewallError
from warden.firewall.backends.base import Backend
from warden.firewall.model import Policy, Rule

APPLYING = "applying a policy"
ADOPTING = "adopting another firewall"

# What can happen to a firewall, as far as anybody watching is concerned.
APPLIED = "applied"
CONFIRMED = "confirmed"
ROLLED_BACK = "rolled_back"
RESTORED = "restored"

# How often the watchdog looks. Short enough that confirming feels immediate,
# long enough that it costs nothing while it waits.
BEAT = 1.0


@dataclass(frozen=True)
class Armed:
    """A rollback that is waiting to happen."""

    snapshot: int
    deadline: datetime
    reason: str | None

    def left(self, now: datetime | None = None) -> float:
        return (self.deadline - (now or datetime.now(UTC))).total_seconds()


def armed(snapshots: Snapshots) -> Armed | None:
    waiting = snapshots.armed()
    return Armed(*waiting) if waiting else None


@dataclass(frozen=True)
class Pending:
    """What is written down that is not in the kernel, and the other way round."""

    added: list[str]
    removed: list[str]
    applied_at: datetime | None
    # True where warden cannot know: it has never applied from here, or a
    # rollback put back a ruleset it did not compose.
    unknown: bool = False

    def __bool__(self) -> bool:
        return bool(self.added or self.removed)

    @property
    def count(self) -> int:
        return len(self.added) + len(self.removed)


def pending(rules: list[Rule], snapshots: Snapshots) -> Pending:
    """Which rules have changed since the last apply.

    Written down is not applied, and after `warden firewall allow` nothing says
    so again. On a machine where three rules have been sitting in the book since
    yesterday, that is the difference between a firewall and a list.
    """
    written = sorted(rule.name for rule in rules)
    live = snapshots.live()
    if live is None:
        return Pending(added=written, removed=[], applied_at=None, unknown=True)
    at, applied = live
    return Pending(
        added=[name for name in written if name not in applied],
        removed=[name for name in applied if name not in written],
        applied_at=at,
    )


def apply(
    backend: Backend,
    snapshots: Snapshots,
    policy: Policy,
    *,
    rollback: int,
    reason: str = APPLYING,
) -> Armed | None:
    """Snapshot, arm, apply. Returns the rollback that is now waiting, if any.

    The order is not negotiable. A snapshot taken after the change describes
    the change, not what to go back to, and a rollback armed after the apply
    leaves a window where a lost session means a lost machine.
    """
    before = snapshots.take(backend.kind, backend.snapshot(), reason)
    waiting = None
    if rollback > 0:
        deadline = datetime.now(UTC) + timedelta(seconds=rollback)
        snapshots.arm(before, deadline, reason)
        waiting = Armed(before, deadline, reason)

    try:
        backend.apply(policy)
    except Exception:
        # Nothing was applied, so nothing should be waiting to be undone.
        snapshots.disarm()
        raise
    snapshots.went_live([rule.name for rule in policy.rules])
    snapshots.tally(APPLIED)
    snapshots.said(
        APPLIED,
        backend.kind,
        rules=len(policy.live(datetime.now(UTC))),
        rollback=rollback,
        reason=reason,
    )
    return waiting


def confirm(snapshots: Snapshots) -> Armed:
    """Keep what was applied. Raises when there was nothing waiting."""
    waiting = armed(snapshots)
    if waiting is None:
        raise FirewallError("nothing is waiting to be rolled back")
    snapshots.disarm()
    snapshots.said(CONFIRMED, str(waiting.reason or ""), snapshot=waiting.snapshot)
    return waiting


def roll_back(backend: Backend, snapshots: Snapshots, snapshot: int | None = None) -> int:
    """Put back a snapshot, and stop any rollback that was waiting for it."""
    if snapshot is None:
        waiting = armed(snapshots)
        if waiting is None:
            latest = snapshots.latest()
            if latest is None:
                raise FirewallError("there is no snapshot to go back to")
            snapshot = int(latest["id"])
        else:
            snapshot = waiting.snapshot

    body = snapshots.body(snapshot)
    if body is None:
        raise FirewallError(f"no snapshot {snapshot}")
    backend.restore(body)
    # What is in the kernel now is whatever was there before, which warden did
    # not write and cannot name. Saying nothing beats saying something wrong.
    snapshots.forget_live()
    was_waiting = snapshots.disarm()
    snapshots.tally(ROLLED_BACK if was_waiting else RESTORED)
    snapshots.said(
        ROLLED_BACK if was_waiting else RESTORED, backend.kind, snapshot=snapshot
    )
    return snapshot


def wait_out(backend: Backend, snapshots: Snapshots, until: datetime) -> bool:
    """Sit out the window, and roll back if nobody confirmed. True if it did.

    The store is re-read each beat rather than held in a variable: the command
    that confirms is quite likely the only thing still alive to do it.
    """
    while datetime.now(UTC) < until:
        if snapshots.armed() is None:
            return False
        _sleep(BEAT)
    if snapshots.armed() is None:
        return False
    roll_back(backend, snapshots)
    return True


def watch(database: str, until: datetime) -> None:
    """Open this machine's database and sit out the window.

    Runs detached, because the whole point is outliving the session that armed
    it.
    """
    from warden.core.store import Store
    from warden.firewall.backends.base import backend_for

    with Store(database) as store:
        wait_out(backend_for(), Snapshots(store), until)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def start_watchdog(database: str, until: datetime) -> int | None:
    """Leave something behind that outlives this command and this session."""
    arguments = [
        sys.executable,
        "-m",
        "warden",
        "firewall",
        "_watch",
        "--database",
        str(database),
        "--until",
        until.isoformat(),
    ]
    detach: dict[str, object] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        detach["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        # Its own session, so the hangup that closes ssh does not reach it.
        detach["start_new_session"] = True
    try:
        return subprocess.Popen(arguments, **detach).pid  # type: ignore[arg-type]
    except OSError:
        return None

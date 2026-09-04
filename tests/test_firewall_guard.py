"""The rollback, which is the part that decides whether a mistake is recoverable."""

from datetime import UTC, datetime, timedelta

import pytest

from warden.core.store import Snapshots, Store
from warden.errors import FirewallError, NotPermittedError
from warden.firewall import guard
from warden.firewall.backends.base import Backend
from warden.firewall.model import Policy, Rule


class Machine(Backend):
    """A firewall that remembers what it was told, and can be made to refuse."""

    kind = "test"

    def __init__(self, refuses: bool = False) -> None:
        self.loaded = "table inet before {}"
        self.applied: list[Policy] = []
        self.refuses = refuses

    def available(self) -> bool:
        return True

    def render(self, policy: Policy) -> str:
        return f"# {len(policy.rules)} rules"

    def apply(self, policy: Policy) -> None:
        if self.refuses:
            raise NotPermittedError("a firewall needs root")
        self.applied.append(policy)
        self.loaded = self.render(policy)

    def snapshot(self) -> str:
        return self.loaded

    def restore(self, snapshot: str) -> None:
        self.loaded = snapshot


@pytest.fixture
def snapshots():
    with Store(":memory:") as store:
        yield Snapshots(store)


def policy(*names: str) -> Policy:
    return Policy(rules=[Rule(name=name, ports={8000}) for name in names])


def test_applying_takes_a_snapshot_of_what_was_there_first(snapshots: Snapshots):
    machine = Machine()
    guard.apply(machine, snapshots, policy("a"), rollback=60)
    kept = snapshots.body(1)
    assert kept == "table inet before {}"
    assert machine.loaded == "# 1 rules"


def test_applying_arms_a_rollback_before_it_changes_anything(snapshots: Snapshots):
    """Armed after the apply would leave a window where a lost session is fatal."""
    waiting = guard.apply(Machine(), snapshots, policy("a"), rollback=60)
    assert waiting is not None
    assert snapshots.armed() is not None
    assert 0 < waiting.left() <= 60


def test_a_refused_apply_leaves_nothing_waiting_to_be_undone(snapshots: Snapshots):
    with pytest.raises(NotPermittedError):
        guard.apply(Machine(refuses=True), snapshots, policy("a"), rollback=60)
    assert snapshots.armed() is None


def test_asking_for_no_rollback_arms_none(snapshots: Snapshots):
    assert guard.apply(Machine(), snapshots, policy("a"), rollback=0) is None
    assert snapshots.armed() is None


def test_confirming_keeps_what_was_applied(snapshots: Snapshots):
    machine = Machine()
    guard.apply(machine, snapshots, policy("a"), rollback=60)
    guard.confirm(snapshots)
    assert snapshots.armed() is None
    assert machine.loaded == "# 1 rules"


def test_confirming_when_nothing_is_waiting_says_so(snapshots: Snapshots):
    with pytest.raises(FirewallError, match="nothing is waiting"):
        guard.confirm(snapshots)


def test_rolling_back_puts_the_machine_where_it_was(snapshots: Snapshots):
    machine = Machine()
    guard.apply(machine, snapshots, policy("a"), rollback=60)
    assert machine.loaded == "# 1 rules"
    guard.roll_back(machine, snapshots)
    assert machine.loaded == "table inet before {}"
    assert snapshots.armed() is None


def test_rolling_back_without_a_snapshot_says_so(snapshots: Snapshots):
    with pytest.raises(FirewallError, match="no snapshot to go back to"):
        guard.roll_back(Machine(), snapshots)


def test_the_watchdog_leaves_quietly_when_somebody_confirmed(snapshots: Snapshots):
    machine = Machine()
    guard.apply(machine, snapshots, policy("a"), rollback=60)
    guard.confirm(snapshots)
    assert guard.wait_out(machine, snapshots, datetime.now(UTC) + timedelta(seconds=5)) is False
    assert machine.loaded == "# 1 rules"


def test_nobody_confirming_puts_the_machine_back(snapshots: Snapshots):
    """The test the whole feature exists for."""
    machine = Machine()
    guard.apply(machine, snapshots, policy("a"), rollback=60)
    assert machine.loaded == "# 1 rules"

    went_back = guard.wait_out(machine, snapshots, datetime.now(UTC) - timedelta(seconds=1))

    assert went_back is True
    assert machine.loaded == "table inet before {}"
    assert snapshots.armed() is None


def test_the_watchdog_waits_before_it_gives_up(snapshots: Snapshots, monkeypatch):
    """It must not roll back the instant it starts, or the window is nothing."""
    beats = []
    monkeypatch.setattr(guard, "_sleep", lambda seconds: beats.append(seconds))
    machine = Machine()
    guard.apply(machine, snapshots, policy("a"), rollback=60)

    def confirm_after_two(seconds):
        beats.append(seconds)
        if len(beats) == 2:
            snapshots.disarm()

    monkeypatch.setattr(guard, "_sleep", confirm_after_two)
    assert guard.wait_out(machine, snapshots, datetime.now(UTC) + timedelta(seconds=60)) is False
    assert len(beats) == 2
    assert machine.loaded == "# 1 rules"

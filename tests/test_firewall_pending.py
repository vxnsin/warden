"""Written down is not applied, and something has to say so."""

from datetime import UTC, datetime, timedelta

import pytest

from warden.core.store import RuleStore, Snapshots, Store
from warden.firewall import guard
from warden.firewall.model import Action, Direction, Origin, Protocol, Rule


def rule(name: str, port: int = 8000) -> Rule:
    return Rule(
        name=name,
        direction=Direction.IN,
        action=Action.ALLOW,
        protocol=Protocol.TCP,
        ports={port},
        source="10.0.0.0/8",
        origin=Origin.MANUAL,
    )


@pytest.fixture
def books(tmp_path) -> tuple[RuleStore, Snapshots]:
    with Store(tmp_path / "registry.db") as store:
        yield RuleStore(store), Snapshots(store)


def test_a_machine_that_has_never_applied_says_so(books):
    rules, snapshots = books
    rules.save(rule("allow-ssh"))
    found = guard.pending(rules.list(), snapshots)
    assert found.unknown
    assert found.added == ["allow-ssh"]
    assert found.applied_at is None


def test_nothing_changed_after_an_apply(books):
    rules, snapshots = books
    rules.save(rule("allow-ssh"))
    snapshots.went_live(["allow-ssh"])
    found = guard.pending(rules.list(), snapshots)
    assert not found
    assert found.count == 0
    assert found.applied_at is not None


def test_a_rule_written_since_the_apply_is_added(books):
    rules, snapshots = books
    rules.save(rule("allow-ssh"))
    snapshots.went_live(["allow-ssh"])
    rules.save(rule("allow-https", 443))
    found = guard.pending(rules.list(), snapshots)
    assert found.added == ["allow-https"]
    assert found.removed == []
    assert bool(found)


def test_a_rule_closed_since_the_apply_is_removed(books):
    """The one a timestamp could never have found: it is not there any more."""
    rules, snapshots = books
    rules.save(rule("allow-ssh"))
    rules.save(rule("allow-https", 443))
    snapshots.went_live(["allow-ssh", "allow-https"])
    rules.delete("allow-https")
    found = guard.pending(rules.list(), snapshots)
    assert found.added == []
    assert found.removed == ["allow-https"]
    assert found.count == 1


def test_both_at_once(books):
    rules, snapshots = books
    snapshots.went_live(["allow-ssh"])
    rules.save(rule("allow-https", 443))
    found = guard.pending(rules.list(), snapshots)
    assert found.added == ["allow-https"]
    assert found.removed == ["allow-ssh"]
    assert found.count == 2


def test_a_rollback_makes_it_unknown_again(books):
    """What a snapshot puts back is a ruleset warden did not compose."""
    rules, snapshots = books
    rules.save(rule("allow-ssh"))
    snapshots.went_live(["allow-ssh"])
    assert not guard.pending(rules.list(), snapshots)

    snapshots.forget_live()
    found = guard.pending(rules.list(), snapshots)
    assert found.unknown
    assert found.added == ["allow-ssh"]


def test_applying_writes_down_what_went_live(books, monkeypatch):
    from warden.firewall.model import Policy

    rules, snapshots = books
    rules.save(rule("allow-ssh"))
    rules.save(rule("allow-https", 443))

    class Pretend:
        kind = "nftables"

        def snapshot(self) -> str:
            return "before"

        def apply(self, policy) -> None:
            return None

    guard.apply(Pretend(), snapshots, Policy(rules=rules.list()), rollback=0)
    assert not guard.pending(rules.list(), snapshots)
    at, names = snapshots.live()
    assert names == ["allow-https", "allow-ssh"]
    assert datetime.now(UTC) - at < timedelta(seconds=5)


def test_a_failed_apply_leaves_the_book_alone(books):
    """Nothing went live, so nothing should look as though it did."""
    from warden.firewall.model import Policy

    rules, snapshots = books
    rules.save(rule("allow-ssh"))

    class Refuses:
        kind = "nftables"

        def snapshot(self) -> str:
            return "before"

        def apply(self, policy) -> None:
            raise RuntimeError("nft refused")

    with pytest.raises(RuntimeError):
        guard.apply(Refuses(), snapshots, Policy(rules=rules.list()), rollback=0)
    assert guard.pending(rules.list(), snapshots).unknown

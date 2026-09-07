"""A rule that closes itself, whoever wrote it."""

from datetime import UTC, datetime, timedelta

import pytest

from warden.core.config import Settings
from warden.firewall import bounds, catalogue, link
from warden.firewall.model import Action, Origin, Rule, span


def now() -> datetime:
    """Asked each time. A moment captured at import drifts across a long run."""
    return datetime.now(UTC)


def test_a_span_is_a_number_and_a_unit():
    assert span("30s") == 30
    assert span("15m") == 900
    assert span("2h") == 7200
    assert span("1d") == 86400


def test_anything_else_says_what_one_looks_like():
    for said in ("a while", "2 hours", "0h", "-1m", "2w", ""):
        with pytest.raises(ValueError, match="length of time"):
            span(said)


def test_a_rule_can_be_given_a_clock():
    made = catalogue.rule_for("8443", action=Action.ALLOW, for_seconds=7200)
    assert made.expires_at is not None
    left = made.expires_at - now()
    assert timedelta(hours=1, minutes=59) < left <= timedelta(hours=2)


def test_a_rule_without_one_has_none():
    assert catalogue.rule_for("8443", action=Action.ALLOW).expires_at is None


def test_an_expired_rule_is_swept_whoever_wrote_it():
    """It used to take a borrowed lease to be swept. A clock is enough now."""
    lapsed = Rule(
        name="allow-8443",
        ports={8443},
        origin=Origin.MANUAL,
        expires_at=now() - timedelta(minutes=1),
    )
    assert bounds.closed_by([], [lapsed], now()) == [lapsed]


def test_a_rule_whose_clock_is_still_running_is_left_alone():
    running = Rule(
        name="allow-8443",
        ports={8443},
        origin=Origin.MANUAL,
        expires_at=now() + timedelta(hours=1),
    )
    assert bounds.closed_by([], [running], now()) == []


def test_a_manual_rule_with_no_clock_is_never_swept():
    """Nothing about it says when it should go, so nothing decides that it has."""
    forever = Rule(name="allow-8443", ports={8443}, origin=Origin.MANUAL)
    assert bounds.closed_by([], [forever], now()) == []


def test_a_borrowed_lease_still_closes_the_way_it_did():
    borrowed = Rule(
        name="allow-shop-api",
        ports={8000},
        origin=Origin.REGISTRY,
        service="shop-api",
    )
    assert bounds.closed_by([], [borrowed], now()) == [borrowed]


def test_reconcile_names_an_expired_manual_rule_too():
    lapsed = Rule(
        name="allow-8443",
        ports={8443},
        origin=Origin.MANUAL,
        expires_at=now() - timedelta(minutes=1),
    )
    assert link.reconcile([lapsed], [], now()) == ["allow-8443"]


def test_doctor_says_how_long_the_first_one_has(tmp_path, monkeypatch):
    from warden.core.health import NOTE, _firewall
    from warden.core.store import RuleStore, Store

    where = tmp_path / "rules.db"
    monkeypatch.setenv("WARDEN_DATABASE", str(where))
    settings = Settings(database=where, update_check=False)
    with Store(where) as store:
        rules = RuleStore(store)
        rules.save(
            Rule(name="allow-8443", ports={8443}, expires_at=now() + timedelta(hours=2))
        )
        rules.save(
            Rule(name="allow-9000", ports={9000}, expires_at=now() + timedelta(minutes=20))
        )

    said = [check.text for check in _firewall(settings) if check.level == NOTE]
    assert any("2 rules closing on their own" in one for one in said)
    assert any("the first of them in 19m" in one or "in 20m" in one for one in said)


def test_a_rule_with_no_clock_says_nothing_in_doctor(tmp_path, monkeypatch):
    from warden.core.health import _firewall
    from warden.core.store import RuleStore, Store

    where = tmp_path / "rules.db"
    monkeypatch.setenv("WARDEN_DATABASE", str(where))
    settings = Settings(database=where, update_check=False)
    with Store(where) as store:
        RuleStore(store).save(Rule(name="allow-8443", ports={8443}))

    said = [check.text for check in _firewall(settings)]
    assert not any("closing on their own" in one for one in said)

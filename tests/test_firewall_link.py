"""The registry and the firewall meeting, which is the reason they share a program."""

from datetime import UTC, datetime, timedelta

import pytest

from warden.core.config import Settings
from warden.errors import NotPermittedError, UnknownServiceError
from warden.firewall import link
from warden.firewall.model import Origin, Protocol, Rule, runs, spelled
from warden.models import Registration


def settings(**overrides) -> Settings:
    fields = {
        "pool_start": 8000,
        "pool_end": 8999,
        "firewall_from_registry": True,
        "firewall_allow_from": "10.0.0.0/8",
        "update_check": False,
    }
    return Settings(**{**fields, **overrides})


def service(**overrides) -> Registration:
    now = datetime.now(UTC)
    fields = {
        "name": "shop-api",
        "kind": "backend",
        "project": "shop",
        "host": "0.0.0.0",
        "port": 8000,
        "pid": None,
        "meta": {},
        "ttl": None,
        "created_at": now,
        "updated_at": now,
        "expires_at": None,
    }
    return Registration(**{**fields, **overrides})


def test_a_rule_takes_the_port_the_registry_actually_handed_out():
    rule = link.rule_for(service(port=8042), source="10.0.0.0/8", settings=settings())
    assert rule.ports == {8042}
    assert rule.service == "shop-api"
    assert rule.origin is Origin.REGISTRY


def test_the_rule_inherits_the_lease_rather_than_outliving_it():
    now = datetime.now(UTC)
    leased = service(ttl=60, expires_at=now + timedelta(seconds=60))
    rule = link.rule_for(leased, source="10.0.0.0/8", settings=settings())
    assert rule.expires_at == leased.expires_at


def test_a_service_the_registry_does_not_know_is_refused():
    with pytest.raises(UnknownServiceError, match="no service registered as 'ghost'"):
        link.found([service()], "ghost")


def test_a_rule_from_the_registry_still_has_to_pass_the_bounds():
    """The link is not a way around them - it is the only way through them."""
    with pytest.raises(NotPermittedError, match="nothing outside this machine can reach"):
        link.rule_for(service(host="127.0.0.1"), source="10.0.0.0/8", settings=settings())

    with pytest.raises(NotPermittedError, match="may not open a port to anywhere"):
        link.rule_for(service(), source="any", settings=settings())

    with pytest.raises(NotPermittedError, match="may not open ports here"):
        link.rule_for(
            service(), source="10.0.0.0/8", settings=settings(firewall_from_registry=False)
        )


def test_a_development_window_covers_the_pool_and_nothing_beyond_it():
    window = link.window(settings(), "10.0.0.0/8", 7200)
    assert min(window.ports) == 8000
    assert max(window.ports) == 8999
    assert 22 not in window.ports
    assert 3389 not in window.ports


def test_a_development_window_says_when_it_ends():
    window = link.window(settings(), "10.0.0.0/8", 60)
    assert window.expires_at is not None
    assert 0 < (window.expires_at - datetime.now(UTC)).total_seconds() <= 60


def test_a_development_window_is_bounded_like_everything_else():
    with pytest.raises(NotPermittedError, match="no networks are declared"):
        link.window(settings(firewall_allow_from=""), "10.0.0.0/8", 60)


def test_a_rule_whose_service_is_gone_is_named_for_closing():
    rule = link.rule_for(service(), source="10.0.0.0/8", settings=settings())
    assert link.reconcile([rule], []) == ["allow-shop-api"]


def test_a_rule_whose_service_is_still_there_is_left_alone():
    rule = link.rule_for(service(), source="10.0.0.0/8", settings=settings())
    assert link.reconcile([rule], [service()]) == []


def test_a_development_window_is_not_closed_for_want_of_a_service():
    """It belongs to a person, not to a registration, so it waits for its clock."""
    window = link.window(settings(), "10.0.0.0/8", 3600)
    assert link.reconcile([window], []) == []


def test_a_development_window_that_has_lapsed_is_named_for_closing():
    window = link.window(settings(), "10.0.0.0/8", 60)
    later = datetime.now(UTC) + timedelta(seconds=61)
    assert link.reconcile([window], [], later) == ["dev-mode"]


def test_a_rule_a_person_wrote_is_never_closed_by_the_sweep():
    mine = Rule(name="ssh", ports={22}, protocol=Protocol.TCP)
    assert link.reconcile([mine], []) == []


def test_a_thousand_ports_in_a_row_are_one_range():
    assert spelled(set(range(8000, 9000))) == "8000-8999"
    assert spelled({80, 443}) == "80,443"
    assert spelled({80, 81, 82, 443}) == "80-82,443"
    assert runs(set()) == []


def test_doctor_says_when_the_pool_is_open_and_for_how_much_longer(tmp_path, monkeypatch):
    """A window that outlives its afternoon is what this is here to prevent."""
    from warden.core.health import WARN, _firewall
    from warden.core.store import RuleStore, Store

    monkeypatch.setenv("WARDEN_DATABASE", str(tmp_path / "rules.db"))
    where = settings(database=tmp_path / "rules.db")
    with Store(where.database) as store:
        RuleStore(store).save(link.window(where, "10.0.0.0/8", 3600))

    checks = _firewall(where)
    assert [check.level for check in checks] == [WARN]
    assert "the pool is open to 10.0.0.0/8" in checks[0].text


def test_doctor_counts_what_the_registry_opened(tmp_path, monkeypatch):
    from warden.core.health import NOTE, _firewall
    from warden.core.store import RuleStore, Store

    monkeypatch.setenv("WARDEN_DATABASE", str(tmp_path / "rules.db"))
    where = settings(database=tmp_path / "rules.db")
    with Store(where.database) as store:
        RuleStore(store).save(
            link.rule_for(service(), source="10.0.0.0/8", settings=where)
        )

    checks = _firewall(where)
    assert [check.level for check in checks] == [NOTE]
    assert "1 rule opened for a registered service" in checks[0].text


def test_doctor_says_nothing_about_a_machine_with_no_rules(tmp_path):
    from warden.core.health import _firewall

    assert _firewall(settings(database=tmp_path / "empty.db")) == []

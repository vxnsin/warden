"""One test per bound, each of them trying to get through it."""

from datetime import UTC, datetime, timedelta

import pytest

from warden.core.config import Settings
from warden.errors import NotPermittedError
from warden.firewall.bounds import closed_by, permitted
from warden.firewall.model import Origin, Rule
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


def rule(**overrides) -> Rule:
    fields = {
        "name": "shop-api",
        "ports": {8000},
        "source": "10.0.0.0/8",
        "origin": Origin.REGISTRY,
        "service": "shop-api",
    }
    return Rule(**{**fields, **overrides})


def service(**overrides) -> Registration:
    now = datetime.now(UTC)
    fields = {
        "name": "shop-api",
        "kind": "backend",
        "project": None,
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


def test_a_rule_within_every_bound_is_allowed():
    permitted(rule(), settings(), service())


def test_a_rule_a_person_wrote_is_not_second_guessed():
    """The bounds hold the registry at arm's length, not the operator."""
    permitted(rule(origin=Origin.MANUAL, ports={22}, source="any"), settings())


def test_the_registry_cannot_open_ssh():
    """The bound that matters most: 22 is outside the pool, so it is unreachable."""
    with pytest.raises(NotPermittedError, match="22 is outside 8000-8999"):
        permitted(rule(ports={22}), settings(), service(port=22))


def test_the_registry_cannot_step_outside_the_pool_at_all():
    for port in (7999, 9000, 3389, 445):
        with pytest.raises(NotPermittedError, match="only open ports it hands out"):
            permitted(rule(ports={port}), settings())


def test_a_rule_that_names_no_port_is_refused():
    with pytest.raises(NotPermittedError, match="has to name the port"):
        permitted(rule(ports=set(), protocol="tcp"), settings())


def test_nothing_is_allowed_until_it_is_switched_on():
    with pytest.raises(NotPermittedError, match="may not open ports here"):
        permitted(rule(), settings(firewall_from_registry=False), service())


def test_nothing_declared_is_nothing_allowed():
    with pytest.raises(NotPermittedError, match="no networks are declared"):
        permitted(rule(), settings(firewall_allow_from=""), service())


def test_the_registry_cannot_open_a_port_to_anywhere():
    with pytest.raises(NotPermittedError, match="may not open a port to anywhere"):
        permitted(rule(source="any"), settings(), service())


def test_a_source_outside_the_declared_networks_is_refused():
    with pytest.raises(NotPermittedError, match=r"is not inside 10.0.0.0/8"):
        permitted(rule(source="192.168.4.0/24"), settings(), service())


def test_a_narrower_source_inside_a_declared_network_is_fine():
    permitted(rule(source="10.4.0.0/16"), settings(), service())


def test_a_service_on_loopback_has_nothing_to_open():
    with pytest.raises(NotPermittedError, match="nothing outside this machine can reach"):
        permitted(rule(), settings(), service(host="127.0.0.1"))


def test_a_leased_service_may_not_have_an_unleased_rule():
    leased = service(ttl=60, expires_at=datetime.now(UTC) + timedelta(seconds=60))
    with pytest.raises(NotPermittedError, match="has to expire with it"):
        permitted(rule(), settings(), leased)


def test_a_rule_may_not_outlive_the_lease_it_borrowed():
    now = datetime.now(UTC)
    leased = service(ttl=60, expires_at=now + timedelta(seconds=60))
    with pytest.raises(NotPermittedError, match="would outlive"):
        permitted(rule(expires_at=now + timedelta(hours=1)), settings(), leased)


def test_a_rule_that_expires_with_its_lease_is_fine():
    now = datetime.now(UTC)
    leased = service(ttl=60, expires_at=now + timedelta(seconds=60))
    permitted(rule(expires_at=leased.expires_at), settings(), leased)


def test_a_rule_whose_service_is_gone_is_named_for_closing():
    gone = closed_by([], [rule()], datetime.now(UTC))
    assert [r.name for r in gone] == ["shop-api"]


def test_a_rule_whose_service_moved_port_is_named_for_closing():
    """The port went back to the pool. The way in must not stay behind."""
    moved = closed_by([service(port=8005)], [rule(ports={8000})], datetime.now(UTC))
    assert len(moved) == 1


def test_a_rule_whose_service_is_still_there_is_left_alone():
    assert closed_by([service()], [rule()], datetime.now(UTC)) == []


def test_a_rule_a_person_wrote_is_never_closed_by_the_sweep():
    mine = rule(origin=Origin.MANUAL, service=None)
    assert closed_by([], [mine], datetime.now(UTC)) == []

"""What warden does with IPv6, written down so it is a decision rather than luck.

Two places already handled it before anybody asked: `bounds._within` catches the
comparison between a v4 network and a v6 one, and `adopt._already` drops the v6
twin ufw writes beside every v4 rule. This file is the rest of the answer.
"""

from datetime import UTC, datetime, timedelta

import pytest

from warden.core.config import Settings
from warden.errors import NotPermittedError
from warden.firewall import bounds, link
from warden.firewall.backends import iptables, nftables, pf, windows
from warden.firewall.model import Origin, Rule
from warden.models import Registration

NOW = datetime.now(UTC)


def settings(**more) -> Settings:
    said = {
        "firewall_from_registry": True,
        "firewall_allow_from": {"2001:db8::/32"},
        "pool_start": 8000,
        "pool_end": 8999,
        "update_check": False,
    }
    return Settings(**{**said, **more})


def service(host: str = "2001:db8:1::7", port: int = 8000, **more) -> Registration:
    return Registration(
        name="shop-api",
        kind="backend",
        host=host,
        port=port,
        project=None,
        pid=None,
        meta={},
        ttl=None,
        created_at=NOW,
        updated_at=NOW,
        expires_at=more.pop("expires_at", None),
        **more,
    )


def rule(**more) -> Rule:
    said = {"name": "allow-shop-api", "ports": {8000}, "source": "2001:db8:1::/48"}
    return Rule(**{**said, **more, "origin": more.pop("origin", Origin.REGISTRY)})


# The eight bounds, each asked with a v6 address.


def test_a_v6_network_can_be_declared_and_a_v6_source_is_inside_it():
    bounds.permitted(rule(), settings())


def test_a_v6_source_outside_the_declared_network_is_refused():
    with pytest.raises(NotPermittedError, match="not inside"):
        bounds.permitted(rule(source="2001:db9::/32"), settings())


def test_a_v6_source_is_not_inside_a_v4_network_and_the_other_way_round():
    """Neither contains the other, and comparing them raises rather than matches."""
    with pytest.raises(NotPermittedError, match="not inside"):
        bounds.permitted(rule(), settings(firewall_allow_from={"10.0.0.0/8"}))
    with pytest.raises(NotPermittedError, match="not inside"):
        bounds.permitted(rule(source="10.1.0.0/16"), settings())


def test_both_families_can_be_declared_at_once():
    said = settings(firewall_allow_from={"10.0.0.0/8", "2001:db8::/32"})
    bounds.permitted(rule(), said)
    bounds.permitted(rule(source="10.1.0.0/16"), said)


def test_the_pool_bound_does_not_care_which_family_it_is():
    with pytest.raises(NotPermittedError, match="outside 8000-8999"):
        bounds.permitted(rule(ports={22}), settings())


def test_a_service_on_the_v6_loopback_has_nothing_to_open():
    """`::1` is as unreachable from outside as `127.0.0.1`, and says the same."""
    with pytest.raises(NotPermittedError, match="nothing to open"):
        bounds.permitted(rule(), settings(), service(host="::1"))


def test_a_v6_lease_is_still_a_lease():
    lapses = NOW + timedelta(minutes=5)
    held = service(expires_at=lapses)
    with pytest.raises(NotPermittedError, match="outlive"):
        bounds.permitted(
            rule(expires_at=lapses + timedelta(minutes=5)), settings(), held
        )


def test_a_rule_built_for_a_v6_service_carries_its_address():
    made = link.rule_for(service(), source="2001:db8:1::/48", settings=settings())
    assert made.ports == {8000}
    assert made.source == "2001:db8:1::/48"


# What each backend does with it.


def test_nftables_holds_both_families_in_one_ruleset():
    assert "ip6 saddr 2001:db8:1::/48" in nftables.line(rule())
    assert "ip saddr 10.0.0.0/8" in nftables.line(rule(source="10.0.0.0/8"))


def test_pf_takes_a_v6_address_as_it_is():
    assert "from 2001:db8:1::/48" in pf.line(rule())


def test_the_windows_firewall_takes_one_too():
    assert "remoteip=2001:db8:1::/48" in windows.line(rule())


def test_iptables_refuses_by_name_rather_than_writing_a_table_that_will_not_load():
    """`iptables-restore` would refuse the whole table, and every rule in it."""
    with pytest.raises(NotPermittedError, match="IPv4 only"):
        iptables.line(rule())
    with pytest.raises(NotPermittedError, match="IPv4 only"):
        iptables.line(rule(source="any", destination="2001:db8::/32"))


def test_iptables_is_untouched_by_a_v4_rule():
    assert "-s 10.0.0.0/8" in iptables.line(rule(source="10.0.0.0/8"))


def test_a_v6_address_that_is_not_one_is_refused_at_the_model():
    with pytest.raises(ValueError, match="not an address or a network"):
        rule(source="2001:db8::/32 accept")

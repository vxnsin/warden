import pytest
from pydantic import ValidationError

from warden.core.config import Settings, reachable_from_elsewhere, slugify


def test_a_machine_name_is_bent_into_a_usable_node_name():
    assert slugify("BUILD-01") == "build-01"
    assert slugify("build 01.office.lan") == "build-01.office.lan"
    assert slugify("---") == "warden"
    assert slugify("") == "warden"


def test_a_node_name_given_by_hand_is_tidied_too():
    assert Settings(node="Build 01").node == "build-01"


def test_a_warden_advertises_its_own_address_by_default():
    settings = Settings(host="10.0.0.7", port=7010)
    assert settings.advertise_url == "http://10.0.0.7:7010"


def test_an_advertised_address_wins_over_the_listening_one():
    settings = Settings(host="0.0.0.0", advertise="http://build-01:7010/")
    assert settings.advertise_url == "http://build-01:7010"


def test_a_lone_warden_may_sit_on_loopback():
    settings = Settings(host="127.0.0.1")
    assert settings.role == "hub"
    assert settings.upstream is None


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "localhost", "::1"])
def test_a_node_cannot_report_an_address_no_one_else_can_open(address: str):
    with pytest.raises(ValidationError, match="WARDEN_ADVERTISE"):
        Settings(upstream="http://hub:7010", advertise=f"http://{address}:7010")


def test_a_node_with_a_real_address_is_accepted():
    settings = Settings(upstream="http://hub:7010/", advertise="http://build-01:7010")
    assert settings.role == "edge"
    assert settings.upstream == "http://hub:7010"


def test_reachability_is_about_the_host_not_the_scheme():
    assert reachable_from_elsewhere("http://10.0.0.7:7010") is True
    assert reachable_from_elsewhere("https://build-01.office.lan") is True
    assert reachable_from_elsewhere("http://127.0.0.1:7010") is False
    assert reachable_from_elsewhere("http://[::1]:7010") is False


def test_the_lease_a_node_gets_has_sane_bounds():
    assert Settings().node_ttl == 90
    with pytest.raises(ValidationError):
        Settings(node_ttl=1)


def test_two_wardens_on_one_machine_may_both_use_loopback():
    settings = Settings(
        node="build-01",
        port=7020,
        upstream="http://127.0.0.1:7010",
        advertise="http://127.0.0.1:7020",
    )
    assert settings.role == "edge"
    assert settings.advertise_url == "http://127.0.0.1:7020"


def test_a_local_node_needs_no_advertise_at_all():
    settings = Settings(port=7020, upstream="http://127.0.0.1:7010")
    assert settings.advertise_url == "http://127.0.0.1:7020"

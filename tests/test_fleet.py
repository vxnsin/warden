from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from warden.errors import UnknownNodeError
from warden.fleet import Fleet
from warden.models import NodeAnnouncement
from warden.store import Store


def announcement(name: str = "build-01", **kwargs) -> NodeAnnouncement:
    return NodeAnnouncement(
        name=name,
        url=kwargs.pop("url", f"http://{name}:7010"),
        pool_start=kwargs.pop("pool_start", 9000),
        pool_end=kwargs.pop("pool_end", 9099),
        version=kwargs.pop("version", "0.1.0"),
    )


@pytest.fixture
def fleet(store: Store) -> Fleet:
    return Fleet(store, ttl=90)


def test_a_node_announcing_itself_is_new(fleet: Fleet):
    node, created = fleet.announce(announcement())
    assert created is True
    assert node.name == "build-01"
    assert node.pool == "9000-9099"
    assert node.status == "online"


def test_announcing_again_renews_instead_of_duplicating(fleet: Fleet):
    first, _ = fleet.announce(announcement())
    second, created = fleet.announce(announcement())
    assert created is False
    assert second.first_seen == first.first_seen
    assert second.last_seen >= first.last_seen
    assert len(fleet.nodes()) == 1


def test_a_node_that_moved_keeps_its_name_and_gets_the_new_address(fleet: Fleet):
    fleet.announce(announcement())
    node, _ = fleet.announce(announcement(url="http://10.0.0.7:7010"))
    assert node.url == "http://10.0.0.7:7010"


def test_nodes_are_listed_by_name(fleet: Fleet):
    for name in ("web-02", "build-01", "db-03"):
        fleet.announce(announcement(name))
    assert [node.name for node in fleet.nodes()] == ["build-01", "db-03", "web-02"]


def test_a_node_that_stopped_reporting_goes_stale_but_stays(fleet: Fleet):
    node, _ = fleet.announce(announcement())
    lapsed = node.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    fleet.store.save_node(lapsed)
    listed = fleet.nodes()
    assert len(listed) == 1
    assert listed[0].status == "stale"


def test_a_forgotten_node_is_gone(fleet: Fleet):
    fleet.announce(announcement())
    fleet.forget("build-01")
    assert fleet.nodes() == []


def test_forgetting_a_node_that_is_not_there_says_so(fleet: Fleet):
    with pytest.raises(UnknownNodeError, match="no node registered"):
        fleet.forget("build-01")


def test_looking_up_an_unknown_node_fails(fleet: Fleet):
    with pytest.raises(UnknownNodeError):
        fleet.get("build-01")


def test_the_count_is_what_health_reports(fleet: Fleet):
    assert fleet.count() == 0
    fleet.announce(announcement())
    assert fleet.count() == 1


def test_an_address_that_is_not_an_http_url_is_refused():
    with pytest.raises(ValidationError, match="http address"):
        announcement(url="build-01:7010")


def test_a_trailing_slash_is_dropped_so_urls_compare_equal():
    assert announcement(url="http://build-01:7010/").url == "http://build-01:7010"


def test_a_node_name_follows_the_same_rules_as_a_service_name():
    with pytest.raises(ValidationError):
        announcement("Build 01")

"""Everything warden can tell you about, and how a filter picks from it."""

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from warden.cli import app
from warden.core import happenings
from warden.core.config import Settings
from warden.core.store import Store
from warden.fleet.nodes import Fleet
from warden.models import SCOPES, Event, NodeAnnouncement

runner = CliRunner()


def test_every_event_is_named_once_and_belongs_to_a_scope():
    assert len(happenings.NAMES) == len(set(happenings.NAMES))
    assert {one.scope for one in happenings.EVERY} == set(SCOPES)


def test_a_renewal_is_still_not_posted_by_default():
    assert "port.renewed" not in happenings.NOTABLE
    assert "port.registered" in happenings.NOTABLE


def test_the_names_0_2_0_used_are_still_understood():
    """Somebody has webhook_events=registered,released written down already."""
    assert happenings.known("registered") == "port.registered"
    assert happenings.known("expired") == "port.expired"
    assert happenings.known("port.moved") == "port.moved"
    assert happenings.known("exploded") is None


def test_a_filter_takes_a_full_name_a_bare_one_or_a_whole_scope():
    assert happenings.wanted({"port.registered"}, "port", "registered")
    assert happenings.wanted({"registered"}, "port", "registered")
    assert happenings.wanted({"firewall.*"}, "firewall", "rolled_back")
    assert not happenings.wanted({"firewall.*"}, "node", "stale")
    assert not happenings.wanted({"node.stale"}, "node", "joined")


def test_the_settings_take_all_three_ways_of_writing_it():
    assert Settings(webhook_events="firewall.*").webhook_events == {"firewall.*"}
    assert Settings(webhook_events="registered").webhook_events == {"registered"}
    with pytest.raises(ValueError, match="no such event: exploded"):
        Settings(webhook_events="exploded")


def announcement(name: str = "build-01") -> NodeAnnouncement:
    return NodeAnnouncement(
        name=name, url=f"http://{name}:7010", pool_start=8000, pool_end=8999, version="0.3.0"
    )


def test_a_node_reporting_in_is_something_you_can_hear_about():
    with Store(":memory:") as store:
        seen: list[Event] = []
        store.subscribe(seen.append)
        Fleet(store).announce(announcement())
        assert [event.full for event in seen] == ["node.joined"]
        assert seen[0].subject == "build-01"
        assert seen[0].body["url"] == "http://build-01:7010"


def test_a_node_reporting_in_again_is_not():
    with Store(":memory:") as store:
        fleet = Fleet(store)
        fleet.announce(announcement())
        seen: list[Event] = []
        store.subscribe(seen.append)
        fleet.announce(announcement())
        assert seen == []


def test_a_node_going_quiet_is_said_once_and_not_on_every_listing():
    with Store(":memory:") as store:
        fleet = Fleet(store, ttl=-1)  # already past its lease
        fleet.announce(announcement())
        seen: list[Event] = []
        store.subscribe(seen.append)
        fleet.nodes()
        fleet.nodes()
        fleet.nodes()
        assert [event.full for event in seen] == ["node.stale"]


def test_forgetting_a_node_says_so():
    with Store(":memory:") as store:
        fleet = Fleet(store)
        fleet.announce(announcement())
        seen: list[Event] = []
        store.subscribe(seen.append)
        fleet.forget("build-01")
        assert [event.full for event in seen] == ["node.forgotten"]


def test_the_command_lists_everything_that_can_happen():
    said = runner.invoke(app, ["events", "--known"]).stdout
    for name in happenings.NAMES:
        assert name in said


def test_the_list_can_be_read_by_a_machine():
    import json

    said = json.loads(runner.invoke(app, ["events", "--known", "--json"]).stdout)
    assert {one["name"] for one in said} == set(happenings.NAMES)
    assert all("means" in one for one in said)


def test_an_event_that_is_not_about_a_port_still_says_when_it_happened():
    event = Event(at=datetime.now(UTC), scope="firewall", action="applied", subject="nftables")
    assert event.full == "firewall.applied"
    assert event.port == 0
    assert event.subject == "nftables"

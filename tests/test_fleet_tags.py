"""A node says what it is, and a fleet command can act on that and nothing else."""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from warden.api import create_app
from warden.cli import app
from warden.core.config import Settings
from warden.core.store import Store
from warden.fleet.nodes import Fleet
from warden.fleet.upstream import UpstreamReporter
from warden.models import Node, NodeAnnouncement

runner_cli = CliRunner()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database=tmp_path / "warden.db",
        node="hub",
        update_check=False,
        health_watch=False,
        tags="hub",
    )


def announced(name: str, *tags: str) -> dict:
    return {
        "name": name,
        "url": f"http://{name}:7010",
        "pool_start": 9000,
        "pool_end": 9099,
        "version": "0.6.0",
        "tags": list(tags),
    }


def fleet_of(tmp_path, *nodes: tuple[str, list[str]]) -> Fleet:
    fleet = Fleet(Store(tmp_path / "fleet.db"))
    for name, tags in nodes:
        fleet.announce(NodeAnnouncement.model_validate(announced(name, *tags)))
    return fleet


# What a node says it is


def test_a_tag_travels_in_the_announcement_a_node_already_sends(tmp_path):
    fleet = fleet_of(tmp_path, ("web-01", ["web", "eu-west"]))
    assert fleet.get("web-01").tags == ["web", "eu-west"]


def test_it_survives_being_written_down(tmp_path):
    fleet_of(tmp_path, ("web-01", ["web"]))
    again = Fleet(Store(tmp_path / "fleet.db"))
    assert again.get("web-01").tags == ["web"]


def test_a_node_that_says_nothing_is_in_no_group_rather_than_every_group(tmp_path):
    fleet = fleet_of(tmp_path, ("web-01", []))
    assert fleet.get("web-01").tags == []
    assert fleet.tagged("web") == []


def test_a_rebuilt_node_says_what_it_is_now(tmp_path):
    """One source for what a machine is, and it is the machine."""
    fleet = fleet_of(tmp_path, ("web-01", ["web"]))
    fleet.announce(NodeAnnouncement.model_validate(announced("web-01", "db")))
    assert fleet.get("web-01").tags == ["db"]


def test_a_node_reports_its_own_tags_upward(tmp_path):
    said = Settings(
        database=tmp_path / "w.db",
        node="web-01",
        tags="web, eu-west",
        upstream="http://hub:7010",
        advertise="http://web-01:7010",
    )
    assert UpstreamReporter(said).announcement["tags"] == ["eu-west", "web"]


def test_which_nodes_carry_one(tmp_path):
    fleet = fleet_of(
        tmp_path, ("web-01", ["web", "eu-west"]), ("web-02", ["web"]), ("db-01", ["db"])
    )
    assert [node.name for node in fleet.tagged("web")] == ["web-01", "web-02"]
    assert [node.name for node in fleet.tagged("eu-west")] == ["web-01"]
    assert fleet.tags() == ["db", "eu-west", "web"]


def test_a_tag_is_a_word_rather_than_anything_at_all():
    with pytest.raises(ValueError):
        Node(
            name="web-01",
            url="http://web-01:7010",
            pool_start=1,
            pool_end=2,
            version="0.6.0",
            tags=["Web Servers"],
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-01T00:00:00Z",
            expires_at="2026-01-01T00:01:00Z",
        )


def test_an_older_database_reads_back_without_tags(tmp_path):
    import sqlite3

    where = tmp_path / "fleet.db"
    fleet_of(tmp_path, ("web-01", ["web"]))
    with sqlite3.connect(where) as db:
        db.execute("ALTER TABLE nodes DROP COLUMN tags")

    with Store(where) as store:
        assert Fleet(store).get("web-01").tags == []


# What a fleet command does with it


def hub(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings))
    client.__enter__()
    for name, tags in (("web-01", ["web", "eu-west"]), ("web-02", ["web"]), ("db-01", ["db"])):
        client.post("/v1/nodes", json=announced(name, *tags))
    return client


def test_a_tag_picks_out_the_nodes_it_names(settings: Settings):
    with hub(settings) as client:
        said = client.get("/v1/fleet/services", params={"tag": "web"}).json()
        assert [one["node"] for one in said["unreachable"]] == ["web-01", "web-02"]


def test_this_warden_is_only_in_it_when_it_says_it_is(settings: Settings):
    """The hub is a machine like any other, and `--tag web` is not about it."""
    with hub(settings) as client:
        client.post("/v1/services", json={"name": "shop-api", "kind": "backend"})

        web = client.get("/v1/fleet/services", params={"tag": "web"}).json()
        assert [one["name"] for one in web["services"]] == []

        mine = client.get("/v1/fleet/services", params={"tag": "hub"}).json()
        assert [one["name"] for one in mine["services"]] == ["shop-api"]
        assert mine["unreachable"] == []


def test_a_tag_nothing_carries_is_a_refusal_rather_than_an_empty_answer(settings: Settings):
    """An apply that quietly touched no machines is worse than one that would not run."""
    with hub(settings) as client:
        said = client.get("/v1/fleet/services", params={"tag": "nothing"})
        assert said.status_code == 404
        assert "no warden here is tagged 'nothing'" in said.json()["detail"]
        assert "db, eu-west, hub, web" in said.json()["detail"]


def test_no_tag_at_all_is_still_the_whole_fleet(settings: Settings):
    with hub(settings) as client:
        said = client.get("/v1/fleet/services").json()
        assert len(said["unreachable"]) == 3


def test_an_apply_across_a_tag_asks_only_those_nodes(settings: Settings):
    said = settings.model_copy(update={"allow_remote_firewall": True})
    with hub(said) as client:
        found = client.post(
            "/v1/fleet/firewall/apply", params={"tag": "web", "rollback": 60}
        ).json()
        assert sorted(one["node"] for one in found["results"]) == ["web-01", "web-02"]


def test_an_apply_across_a_tag_this_warden_carries_includes_it(settings: Settings):
    said = settings.model_copy(update={"allow_remote_firewall": True})
    with hub(said) as client:
        found = client.post(
            "/v1/fleet/firewall/apply", params={"tag": "hub", "rollback": 60}
        ).json()
        assert [one["node"] for one in found["results"]] == ["hub"]


def test_an_update_across_a_tag_asks_only_those_nodes(settings: Settings):
    with hub(settings) as client:
        found = client.post("/v1/fleet/update", params={"tag": "db"}).json()
        assert [one["node"] for one in found["results"]] == ["db-01"]


def test_a_tag_nothing_carries_refuses_an_apply_too(settings: Settings):
    said = settings.model_copy(update={"allow_remote_firewall": True})
    with hub(said) as client:
        found = client.post("/v1/fleet/firewall/apply", params={"tag": "nothing"})
        assert found.status_code == 404


# What a person types


def answering(monkeypatch, **answers) -> list[dict]:
    asked: list[dict] = []

    class Warden:
        url = "http://127.0.0.1:7010"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __getattr__(self, name):
            def said(*args, **kwargs):
                asked.append({"what": name, **kwargs})
                return answers[name]

            return said

    monkeypatch.setattr("warden.cli.shared._client", lambda url, token: Warden())
    return asked


def test_the_flag_carries_the_tag_to_the_hub(monkeypatch):
    from warden.models import FleetFirewallResult

    asked = answering(
        monkeypatch, firewall_apply_fleet=FleetFirewallResult.model_validate({"results": []})
    )
    runner_cli.invoke(app, ["firewall", "apply", "--fleet", "--tag", "web", "--yes"])
    assert asked == [{"what": "firewall_apply_fleet", "rollback": None, "tag": "web"}]


def test_confirming_carries_it_too(monkeypatch):
    from warden.models import FleetFirewallResult

    asked = answering(
        monkeypatch, firewall_confirm_fleet=FleetFirewallResult.model_validate({"results": []})
    )
    runner_cli.invoke(app, ["firewall", "confirm", "--fleet", "--tag", "web"])
    assert asked == [{"what": "firewall_confirm_fleet", "tag": "web"}]


def test_updating_carries_it(monkeypatch):
    from warden.models import FleetUpdate

    asked = answering(monkeypatch, update_fleet=FleetUpdate.model_validate({"results": []}))
    said = runner_cli.invoke(app, ["update", "--fleet", "--tag", "eu-west", "--yes"])
    assert asked == [{"what": "update_fleet", "tag": "eu-west"}]
    assert said.exit_code == 0


def test_listing_carries_it(monkeypatch):
    from warden.models import FleetServices

    asked = answering(
        monkeypatch,
        fleet_services=FleetServices.model_validate(
            {"services": [], "unreachable": [], "duplicates": []}
        ),
    )
    runner_cli.invoke(app, ["ls", "--all", "--tag", "web"])
    assert asked == [{"what": "fleet_services", "project": None, "kind": None, "tag": "web"}]


def test_a_tag_without_the_fleet_is_a_sentence_about_why_not(monkeypatch):
    said = runner_cli.invoke(app, ["ls", "--tag", "web"])
    assert said.exit_code == 1
    assert "--tag goes with --all" in said.stderr


def test_the_nodes_listing_says_what_each_one_is(monkeypatch, settings: Settings):
    with hub(settings) as client:
        known = client.get("/v1/nodes").json()

    class Warden:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def nodes(self):
            return [Node.model_validate(one) for one in known]

    monkeypatch.setattr("warden.cli.shared._client", lambda url, token: Warden())
    said = runner_cli.invoke(app, ["nodes"])
    assert "TAGS" in said.stdout
    assert "eu-west" in said.stdout

    found = json.loads(runner_cli.invoke(app, ["nodes", "--json"]).stdout)
    assert {one["name"]: one["tags"] for one in found}["db-01"] == ["db"]

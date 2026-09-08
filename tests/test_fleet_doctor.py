"""`warden doctor` for forty machines, each having examined itself."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from warden.api import create_app
from warden.cli import app
from warden.core import health
from warden.core.config import Settings
from warden.fleet import aggregate
from warden.models import FleetReport, Node, Report

runner_cli = CliRunner()


def node(name: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        name=name,
        url=f"http://{name}:7010",
        pool_start=9000,
        pool_end=9099,
        version="0.6.0",
        first_seen=now,
        last_seen=now,
        expires_at=now + timedelta(seconds=90),
    )


def a_report(name: str, worst: str = "ok", says: str = "-") -> dict:
    return {
        "node": name,
        "worst": worst,
        "says": says,
        "checks": [{"level": worst, "text": says}],
    }


def serving(**by_host: object) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        answer = by_host.get(request.url.host)
        if answer is None:
            raise httpx.ConnectError("nobody there")
        return httpx.Response(200, json=answer(str(request.url.path)))

    return httpx.MockTransport(handler)


def gathering(nodes, transport, **kwargs):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.gather_reports(http, nodes, **kwargs)

    return asyncio.run(main())


def mine(**more: object) -> Report:
    return Report.model_validate({**a_report("hub"), **more})


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database=tmp_path / "warden.db",
        node="here",
        update_check=False,
        token="",
        webhook_url="",
        upstream="",
    )


# What one machine says about itself


def test_a_warden_examines_itself_over_its_own_api(settings: Settings):
    with TestClient(create_app(settings)) as client:
        said = client.get("/v1/doctor")

    assert said.status_code == 200
    body = said.json()
    assert body["node"] == "here"
    assert any("answering at" in check["text"] for check in body["checks"])
    assert any("pool 8000-8999" in check["text"] for check in body["checks"])


def test_the_worst_level_and_the_first_line_at_it_come_worked_out(settings: Settings):
    """A fleet view has room for one line a node, so it is decided where the checks are."""
    with TestClient(create_app(settings)) as client:
        body = client.get("/v1/doctor").json()

    checks = [health.Check(one["level"], one["text"]) for one in body["checks"]]
    assert body["worst"] == health.worst(checks)
    if body["worst"] == health.OK:
        assert body["says"] == "-"
    else:
        assert body["says"] in [one["text"] for one in body["checks"]]


def test_a_machine_with_nothing_to_report_says_nothing_rather_than_repeating_itself():
    assert health.says([health.Check(health.OK, "answering")]) == "-"
    assert health.worst([]) == health.OK


def test_the_worst_of_them_is_what_a_node_is_reduced_to():
    checks = [
        health.Check(health.OK, "answering"),
        health.Check(health.WARN, "nearly out of ports"),
        health.Check(health.NOTE, "a newer warden exists"),
    ]
    assert health.worst(checks) == health.WARN
    assert health.says(checks) == "nearly out of ports"


def test_it_needs_a_token_like_any_other_read(tmp_path):
    said = Settings(database=tmp_path / "w.db", token="s3cret", update_check=False)
    with TestClient(create_app(said)) as client:
        assert client.get("/v1/doctor").status_code == 401
        allowed = client.get("/v1/doctor", headers={"Authorization": "Bearer s3cret"})
        assert allowed.status_code == 200


# What the fleet says


def test_every_node_answers_for_itself():
    transport = serving(
        **{
            "build-01": lambda path: a_report("build-01", "warn", "3 rules changed"),
            "web-02": lambda path: a_report("web-02"),
        }
    )
    found = gathering([node("build-01"), node("web-02")], transport, local=mine())
    assert [one.node for one in found.reports] == ["build-01", "hub", "web-02"]
    assert found.reports[0].says == "3 rules changed"
    assert found.unreachable == []


def test_a_node_that_did_not_answer_is_named_rather_than_left_out():
    transport = serving(**{"build-01": lambda path: a_report("build-01")})
    found = gathering([node("build-01"), node("db-03")], transport, local=mine())
    assert [one.node for one in found.reports] == ["build-01", "hub"]
    assert [one.node for one in found.unreachable] == ["db-03"]


def test_the_hub_is_in_its_own_answer_without_being_asked():
    found = gathering([], serving(), local=mine(worst="fail", says="the pool is full"))
    assert [one.node for one in found.reports] == ["hub"]
    assert found.reports[0].says == "the pool is full"


def test_each_node_is_asked_for_its_own_doctor_rather_than_its_parts():
    """Half of what doctor reads only exists on the machine it is about."""
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url.path))
        return httpx.Response(200, json=a_report(request.url.host))

    gathering([node("build-01")], httpx.MockTransport(handler), local=mine())
    assert asked == ["/v1/doctor"]


# What a person sees


def answering(monkeypatch, found: dict) -> None:
    class Warden:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def fleet_doctor(self) -> FleetReport:
            return FleetReport.model_validate(found)

    monkeypatch.setattr("warden.cli.shared._client", lambda url, token: Warden())


def test_the_table_is_one_line_a_node(monkeypatch):
    answering(
        monkeypatch,
        {
            "reports": [
                a_report("build-01", "warn", "3 rules changed since the last apply"),
                a_report("hub"),
            ],
            "unreachable": [{"node": "db-03", "url": "http://db-03:7010", "reason": "refused"}],
        },
    )
    said = runner_cli.invoke(app, ["doctor", "--all"])
    assert said.exit_code == 1  # a node nobody could reach is the loudest thing there is
    assert "build-01" in said.stdout
    assert "3 rules changed" in said.stdout
    assert "could not be reached" in said.stdout
    assert "1 failing, 1 warning, of 3" in said.stdout


def test_a_fleet_with_nothing_wrong_exits_zero(monkeypatch):
    answering(monkeypatch, {"reports": [a_report("hub"), a_report("web-02")], "unreachable": []})
    said = runner_cli.invoke(app, ["doctor", "--all"])
    assert said.exit_code == 0
    assert "0 failing, 0 warning, of 2" in said.stdout


def test_verbose_says_every_line_a_node_had(monkeypatch):
    answering(
        monkeypatch,
        {
            "reports": [
                {
                    "node": "web-02",
                    "worst": "note",
                    "says": "a newer warden exists",
                    "checks": [
                        {"level": "ok", "text": "pool 8000-8999, 3 held"},
                        {"level": "note", "text": "a newer warden exists"},
                    ],
                }
            ],
            "unreachable": [],
        },
    )
    said = runner_cli.invoke(app, ["doctor", "--all", "--verbose"])
    assert "pool 8000-8999" in said.stdout
    assert "a newer warden exists" in said.stdout


def test_the_json_is_the_whole_fleet_report(monkeypatch):
    answering(monkeypatch, {"reports": [a_report("hub", "fail", "gone")], "unreachable": []})
    said = runner_cli.invoke(app, ["doctor", "--all", "--json"])
    assert said.exit_code == 1
    assert json.loads(said.stdout)["reports"][0]["says"] == "gone"

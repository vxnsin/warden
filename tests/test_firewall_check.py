"""Would this get through? Arithmetic over the rules, and nothing applied."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from warden.api import create_app
from warden.cli import app
from warden.core.config import Settings
from warden.firewall.model import (
    Action,
    Asked,
    Direction,
    Policy,
    Protocol,
    Rule,
    decides,
)
from warden.fleet import aggregate
from warden.models import Node, NodeVerdict

runner_cli = CliRunner()


def now() -> datetime:
    return datetime.now(UTC)


def rule(name: str, **more) -> Rule:
    return Rule(**{"name": name, "ports": {8000}, "source": "10.0.0.0/8", **more})


def asked(address: str = "10.0.0.5", port: int = 8000, **more) -> Asked:
    return Asked(address=address, port=port, **more)


def walking(*rules: Rule, **policy):
    return decides(Policy(rules=list(rules), **policy), asked(), now())


# The walk


def test_the_first_rule_that_matches_is_the_one_that_decides():
    found = decides(
        Policy(
            rules=[
                rule("allow-pool", ports=set(range(8000, 9000)), priority=100),
                rule("deny-8000", action=Action.DENY, priority=200),
            ]
        ),
        asked(),
        now(),
    )
    assert found.action is Action.ALLOW
    assert found.rule.name == "allow-pool"


def test_the_order_is_the_answer():
    """The same two rules the other way round say the opposite thing."""
    rules = [
        rule("allow-pool", ports=set(range(8000, 9000)), priority=200),
        rule("deny-8000", action=Action.DENY, priority=100),
    ]
    assert decides(Policy(rules=rules), asked(), now()).rule.name == "deny-8000"


def test_where_nothing_matches_the_default_answers():
    found = walking(rule("allow-ssh", ports={22}))
    assert found.action is Action.DENY
    assert found.rule is None
    assert found.why == "nothing matched, and incoming defaults to deny"


def test_outgoing_has_a_default_of_its_own():
    found = decides(Policy(), asked(direction=Direction.OUT), now())
    assert found.action is Action.ALLOW
    assert "outgoing defaults to allow" in found.why


def test_an_address_outside_the_rules_network_does_not_match():
    assert walking(rule("allow-8000")).rule.name == "allow-8000"
    outside = decides(Policy(rules=[rule("allow-8000")]), asked("203.0.113.9"), now())
    assert outside.rule is None


def test_a_rule_for_anywhere_matches_anything():
    assert walking(rule("allow-8000", source="any")).rule.name == "allow-8000"


def test_the_other_direction_is_a_different_question():
    found = decides(
        Policy(rules=[rule("allow-8000")]), asked(direction=Direction.OUT), now()
    )
    assert found.rule is None


def test_a_udp_question_is_not_answered_by_a_tcp_rule():
    found = decides(Policy(rules=[rule("allow-8000")]), asked(protocol=Protocol.UDP), now())
    assert found.rule is None


def test_a_rule_for_any_protocol_answers_either():
    both = Rule(name="allow-all", protocol=Protocol.ANY, source="10.0.0.0/8")
    assert decides(Policy(rules=[both]), asked(protocol=Protocol.UDP), now()).rule is not None


def test_a_rule_with_no_ports_is_a_rule_about_every_port():
    both = Rule(name="allow-all", protocol=Protocol.ANY, source="10.0.0.0/8")
    assert decides(Policy(rules=[both]), asked(port=51820), now()).rule.name == "allow-all"


def test_a_rule_that_is_switched_off_is_not_asked():
    assert walking(rule("allow-8000", enabled=False)).rule is None


def test_a_rule_whose_clock_has_run_out_is_not_asked():
    gone = rule("allow-8000", expires_at=now() - timedelta(minutes=1))
    assert walking(gone).rule is None


def test_a_rule_that_names_the_other_end_is_named_rather_than_guessed_at():
    """Which address this machine has is not something warden can know."""
    found = walking(rule("allow-8000", destination="192.168.1.4"))
    assert found.rule is None
    assert found.passed_over == ("allow-8000",)


def test_the_reason_says_what_the_rule_is_in_the_terms_it_was_asked_in():
    found = walking(rule("allow-shop-api", service="shop-api"))
    assert found.why == "tcp/8000 from 10.0.0.0/8, opened for shop-api"


def test_an_address_that_is_not_one_is_refused_at_the_question():
    with pytest.raises(ValueError, match="is not an address"):
        Asked(address="nonsense", port=8000)
    with pytest.raises(ValueError):
        Asked(address="10.0.0.5", port=70000)


# Over the API


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database=tmp_path / "warden.db", node="here", update_check=False, health_watch=False
    )


def written(client: TestClient) -> None:
    for said in (
        {"what": "8000", "source": "10.0.0.0/8"},
        {"what": "8080", "action": "deny"},
    ):
        client.post("/v1/firewall/rules", json=said)


def test_the_api_answers_the_same_question(settings: Settings):
    said = settings.model_copy(update={"allow_remote_firewall": True})
    with TestClient(create_app(said)) as client:
        written(client)
        found = client.get(
            "/v1/firewall/check", params={"address": "10.0.0.5", "port": 8000}
        ).json()

    assert found["action"] == "allow"
    assert found["rule"] == "allow-8000"
    assert found["why"] == "tcp/8000 from 10.0.0.0/8"


def test_asking_changes_nothing_so_the_switch_does_not_apply(settings: Settings):
    """Reading is what a token already allows, here as everywhere else."""
    with TestClient(create_app(settings)) as client:
        assert settings.allow_remote_firewall is False
        said = client.get("/v1/firewall/check", params={"address": "10.0.0.5", "port": 8000})
        assert said.status_code == 200
        assert said.json()["action"] == "deny"


def test_an_address_that_is_not_one_comes_back_as_a_422(settings: Settings):
    with TestClient(create_app(settings)) as client:
        said = client.get("/v1/firewall/check", params={"address": "nonsense", "port": 8000})
        assert said.status_code == 422


# Over the fleet


def node(name: str) -> Node:
    at = now()
    return Node(
        name=name,
        url=f"http://{name}:7010",
        pool_start=9000,
        pool_end=9099,
        version="0.6.0",
        first_seen=at,
        last_seen=at,
        expires_at=at + timedelta(seconds=90),
    )


def a_verdict(**more: object) -> dict:
    body = {
        "address": "10.0.0.5",
        "port": 8000,
        "protocol": "tcp",
        "direction": "in",
        "action": "allow",
        "rule": "allow-8000",
        "why": "tcp/8000 from 10.0.0.0/8",
        "passed_over": [],
    }
    return {**body, **more}


def serving(**by_host: object) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        answer = by_host.get(request.url.host)
        if answer is None:
            raise httpx.ConnectError("nobody there")
        return httpx.Response(200, json=answer(request))

    return httpx.MockTransport(handler)


def gathering(nodes, transport, **kwargs):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.gather_verdicts(http, nodes, **kwargs)

    return asyncio.run(main())


def mine(**more: object) -> NodeVerdict:
    return NodeVerdict(node="hub", **a_verdict(**more))


ASKING = {"address": "10.0.0.5", "port": "8000", "protocol": "tcp", "direction": "in"}


def test_every_node_answers_from_its_own_ruleset():
    transport = serving(
        **{
            "build-01": lambda request: a_verdict(),
            "web-02": lambda request: a_verdict(
                action="deny", rule=None, why="nothing matched, and incoming defaults to deny"
            ),
        }
    )
    found = gathering(
        [node("build-01"), node("web-02")], transport, here="hub", local=mine(), params=ASKING
    )
    assert [(one.node, one.action) for one in found.verdicts] == [
        ("build-01", "allow"),
        ("hub", "allow"),
        ("web-02", "deny"),
    ]


def test_the_question_travels_as_it_was_asked():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        return httpx.Response(200, json=a_verdict())

    gathering(
        [node("build-01")],
        httpx.MockTransport(handler),
        here="hub",
        local=mine(),
        params=ASKING,
    )
    assert asked == [ASKING]


def test_a_node_that_did_not_answer_is_named():
    transport = serving(**{"build-01": lambda request: a_verdict()})
    found = gathering(
        [node("build-01"), node("db-03")], transport, here="hub", local=mine(), params=ASKING
    )
    assert [one.node for one in found.unreachable] == ["db-03"]


# At a keyboard


@pytest.fixture
def alone(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("WARDEN_DATABASE", str(tmp_path / "rules.db"))


def write(*args: str):
    return runner_cli.invoke(app, ["firewall", *args], catch_exceptions=False)


def test_it_names_the_rule_that_decided(alone):
    write("allow", "8000", "--from", "10.0.0.0/8")
    write("deny", "8080")
    assert "allowed by allow-8000" in write("check", "10.0.0.5:8000").stdout
    assert "denied by deny-8080" in write("check", "10.0.0.5:8080").stdout
    assert "denied by the policy" in write("check", "203.0.113.9:22").stdout


def test_an_address_and_a_port_or_a_sentence_about_why_not(alone):
    said = runner_cli.invoke(app, ["firewall", "check", "nonsense"])
    assert said.exit_code == 1
    assert "is not an address and a port" in said.stderr

    said = runner_cli.invoke(app, ["firewall", "check", "nonsense:8000"])
    assert said.exit_code == 1
    assert "is not an address" in said.stderr


def test_an_ipv6_address_is_written_the_way_a_port_lets_it_be(alone):
    write("allow", "8000", "--from", "2001:db8::/32")
    assert "allowed by allow-8000" in write("check", "[2001:db8::5]:8000").stdout


def test_the_json_says_which_rule_and_why(alone):
    write("allow", "ssh", "--from", "10.0.0.0/8")
    found = json.loads(write("check", "10.0.0.5:22", "--json").stdout)
    assert found["rule"] == "allow-ssh"
    assert found["action"] == "allow"
    assert found["passed_over"] == []


def test_nothing_is_applied_by_asking(alone, monkeypatch):
    """No syscall, no root: it is arithmetic over rules warden already holds."""
    applied = []
    monkeypatch.setattr("warden.firewall.guard.apply", lambda *a, **k: applied.append(1))
    write("allow", "8000", "--from", "10.0.0.0/8")
    write("check", "10.0.0.5:8000")
    assert applied == []

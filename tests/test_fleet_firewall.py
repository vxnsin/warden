"""The firewall of a whole fleet, seen and driven from the hub."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from warden.api import create_app
from warden.core.config import Settings
from warden.fleet import aggregate
from warden.models import FirewallStatus, Node


def node(name: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        name=name,
        url=f"http://{name}:7010",
        pool_start=9000,
        pool_end=9099,
        version="0.4.1",
        first_seen=now,
        last_seen=now,
        expires_at=now + timedelta(seconds=90),
    )


def a_status(**more: object) -> dict:
    body = {
        "backend": "nftables",
        "available": True,
        "enabled": True,
        "remote": True,
        "rules": 3,
        "live": 3,
        "from_registry": 1,
        "rollback_at": None,
    }
    return {**body, **more}


def a_rule(name: str, **more: object) -> dict:
    body = {
        "name": name,
        "direction": "in",
        "action": "allow",
        "protocol": "tcp",
        "ports": [8000],
        "source": "10.0.0.0/8",
        "destination": "any",
        "interface": None,
        "origin": "registry",
        "service": "shop-api",
        "expires_at": None,
        "comment": None,
        "enabled": True,
    }
    return {**body, **more}


def serving(**by_host: object) -> httpx.MockTransport:
    """Nodes that answer, or raise whatever they were given instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = by_host.get(request.url.host)
        if answer is None:
            raise httpx.ConnectError("nobody there")
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer(str(request.url.path)))

    return httpx.MockTransport(handler)


def gathering(nodes, transport, **kwargs):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.gather_firewalls(http, nodes, **kwargs)

    return asyncio.run(main())


def rules_of(nodes, transport, **kwargs):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.gather_rules(http, nodes, **kwargs)

    return asyncio.run(main())


def mine(**more: object) -> FirewallStatus:
    return FirewallStatus.model_validate(a_status(**more))


def test_every_node_reports_its_own_firewall():
    transport = serving(
        **{
            "build-01": lambda path: a_status(rules=5),
            "web-02": lambda path: a_status(backend="iptables", remote=False),
        }
    )
    found = gathering(
        [node("build-01"), node("web-02")], transport, here="hub", local=mine()
    )
    assert [one.node for one in found.firewalls] == ["build-01", "hub", "web-02"]
    assert dict((one.node, one.rules) for one in found.firewalls)["build-01"] == 5
    assert found.unreachable == []


def test_a_node_that_does_not_answer_is_named_rather_than_left_out():
    transport = serving(**{"build-01": lambda path: a_status()})
    found = gathering(
        [node("build-01"), node("db-03")], transport, here="hub", local=mine()
    )
    assert [one.node for one in found.firewalls] == ["build-01", "hub"]
    assert [one.node for one in found.unreachable] == ["db-03"]
    assert "reached" in found.unreachable[0].reason


def test_this_wardens_own_firewall_is_in_the_answer_without_being_asked():
    """The hub holds one too, and a fleet view that left it out would be a lie."""
    found = gathering([], serving(), here="hub", local=mine(rules=9))
    assert [one.node for one in found.firewalls] == ["hub"]
    assert found.firewalls[0].rules == 9


def test_every_rule_carries_the_node_it_is_on():
    transport = serving(
        **{
            "build-01": lambda path: [a_rule("allow-shop-api")],
            "web-02": lambda path: [a_rule("allow-ssh"), a_rule("allow-https")],
        }
    )
    found = rules_of(
        [node("build-01"), node("web-02")],
        transport,
        here="hub",
        local=[a_rule("allow-hub")],
    )
    assert [(rule["node"], rule["name"]) for rule in found.rules] == [
        ("build-01", "allow-shop-api"),
        ("hub", "allow-hub"),
        ("web-02", "allow-https"),
        ("web-02", "allow-ssh"),
    ]


def test_a_rule_is_passed_through_rather_than_parsed():
    """A hub lists a fleet that may be running a newer warden than itself."""
    transport = serving(
        **{"build-01": lambda path: [a_rule("allow-new", something_from_the_future=True)]}
    )
    found = rules_of([node("build-01")], transport, here="hub", local=[])
    assert found.rules[0]["something_from_the_future"] is True


def test_the_origin_filter_is_passed_on_to_every_node():
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json=[])

    found = rules_of(
        [node("build-01")],
        httpx.MockTransport(handler),
        here="hub",
        local=[],
        origin="registry",
    )
    assert found.rules == []
    assert asked == ["http://build-01:7010/v1/firewall/rules?origin=registry"]


def test_a_node_that_will_not_show_its_rules_is_named():
    transport = serving(**{"build-01": lambda path: [a_rule("allow-shop-api")]})
    found = rules_of([node("build-01"), node("db-03")], transport, here="hub", local=[])
    assert [rule["node"] for rule in found.rules] == ["build-01"]
    assert [one.node for one in found.unreachable] == ["db-03"]


def hub_with(settings, *nodes: str) -> TestClient:
    """A hub that believes in some nodes, whether or not they answer."""
    client = TestClient(create_app(settings))
    client.__enter__()
    for name in nodes:
        client.post(
            "/v1/nodes",
            json={
                "name": name,
                "url": f"http://{name}:7010",
                "pool_start": 9000,
                "pool_end": 9099,
                "version": "0.4.1",
            },
        )
    return client


def test_the_hub_answers_for_itself_when_no_node_does(settings: Settings):
    with TestClient(create_app(settings)) as client:
        body = client.get("/v1/fleet/firewall").json()
        assert [one["node"] for one in body["firewalls"]] == ["hub"]
        assert body["unreachable"] == []


def test_a_node_that_cannot_be_reached_is_named_rather_than_failing_the_call(
    settings: Settings,
):
    """One dead machine must not make the whole fleet look broken."""
    client = hub_with(settings, "build-01")
    try:
        body = client.get("/v1/fleet/firewall").json()
        assert [one["node"] for one in body["firewalls"]] == ["hub"]
        assert [one["node"] for one in body["unreachable"]] == ["build-01"]
    finally:
        client.__exit__(None, None, None)


def test_the_fleet_rules_include_the_hubs_own(settings: Settings):
    allowing = settings.model_copy(
        update={
            "allow_remote_firewall": True,
            "firewall_from_registry": True,
            "firewall_allow_from": {"10.0.0.0/8"},
        }
    )
    with TestClient(create_app(allowing)) as client:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        client.post("/v1/firewall/open", json={"service": "shop-api"})
        body = client.get("/v1/fleet/firewall/rules").json()
        assert [(rule["node"], rule["name"]) for rule in body["rules"]] == [
            ("hub", "allow-shop-api")
        ]


def test_reading_the_fleet_firewall_needs_a_token_when_one_is_set(settings: Settings):
    with TestClient(create_app(settings.model_copy(update={"token": "letmein"}))) as client:
        assert client.get("/v1/fleet/firewall").status_code == 401
        allowed = client.get(
            "/v1/fleet/firewall", headers={"Authorization": "Bearer letmein"}
        )
        assert allowed.status_code == 200


def test_a_protocol_this_warden_has_not_heard_of_is_printed_rather_than_refused():
    """A hub older than its fleet still lists what the fleet sent."""
    from warden.cli.commands import firewall as command

    said = command._spelled({"protocol": "sctp", "ports": [9000]})
    assert said == "sctp/9000"


def test_a_catalogue_service_is_named_even_though_the_rule_arrived_as_json():
    from warden.cli.commands import firewall as command

    assert command._spelled({"protocol": "tcp", "ports": [22]}) == "tcp/22 (ssh)"


def test_a_rule_with_no_ports_says_what_it_is_about():
    from warden.cli.commands import firewall as command

    assert command._spelled({"protocol": "icmp", "ports": []}) == "icmp"


def allowing(settings: Settings, **more: object) -> Settings:
    return settings.model_copy(
        update={
            "allow_remote_firewall": True,
            "firewall_from_registry": True,
            "firewall_allow_from": {"10.0.0.0/8"},
            **more,
        }
    )


def test_the_hub_can_open_on_itself_by_name(settings: Settings):
    """`--node hub` is the same door, not a request that goes out and comes back."""
    with TestClient(create_app(allowing(settings))) as client:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        opened = client.post("/v1/fleet/firewall/hub/open", json={"service": "shop-api"})
        assert opened.status_code == 200
        assert opened.json()["node"] == "hub"
        assert opened.json()["ports"] == [8000]


def test_opening_on_this_node_by_name_still_needs_the_switch(settings: Settings):
    """The fleet path is not a way past the gate on the single-node one.

    Everything else about this warden is set up to allow the rule - the registry
    may open ports, the network is declared - so the only thing left to refuse
    it is the switch, which is exactly what is being tested.
    """
    shut = allowing(settings, allow_remote_firewall=False)
    with TestClient(create_app(shut)) as client:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        refused = client.post("/v1/fleet/firewall/hub/open", json={"service": "shop-api"})
        assert refused.status_code == 403
        assert "allow_remote_firewall" in refused.json()["detail"]
        assert client.get("/v1/firewall/rules").json() == []


def test_deleting_on_this_node_by_name_still_needs_the_switch(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        client.post("/v1/fleet/firewall/hub/open", json={"service": "shop-api"})

    shut = allowing(settings, allow_remote_firewall=False)
    with TestClient(create_app(shut)) as client:
        refused = client.delete("/v1/fleet/firewall/hub/rules/allow-shop-api")
        assert refused.status_code == 403
        assert "allow_remote_firewall" in refused.json()["detail"]


def test_applying_on_this_node_by_name_still_needs_the_switch(settings: Settings):
    shut = allowing(settings, allow_remote_firewall=False)
    with TestClient(create_app(shut)) as client:
        refused = client.post("/v1/fleet/firewall/hub/apply")
        assert refused.status_code == 403
        assert "allow_remote_firewall" in refused.json()["detail"]


def test_a_rule_can_be_taken_out_on_this_node_by_name(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        client.post("/v1/fleet/firewall/hub/open", json={"service": "shop-api"})
        gone = client.delete("/v1/fleet/firewall/hub/rules/allow-shop-api")
        assert gone.status_code == 204
        assert client.get("/v1/firewall/rules").json() == []


def test_a_node_nobody_announced_is_a_404(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        missing = client.post("/v1/fleet/firewall/ghost/open", json={"service": "shop-api"})
        assert missing.status_code == 404


def test_only_three_things_can_be_done_to_a_nodes_firewall(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        nonsense = client.post("/v1/fleet/firewall/hub/burn")
        assert nonsense.status_code == 404
        assert "apply, confirm, restore" in nonsense.json()["detail"]


def test_confirming_on_a_node_with_nothing_waiting_says_so(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        said = client.post("/v1/fleet/firewall/hub/confirm")
        assert said.status_code >= 400
        assert "nothing is waiting" in said.json()["detail"]


def test_the_bounds_hold_through_the_hub_exactly_as_they_do_without_it(settings: Settings):
    """The switch says who may ask. The bounds still say what may be asked for."""
    with TestClient(create_app(allowing(settings))) as client:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        outside = client.post(
            "/v1/fleet/firewall/hub/open",
            json={"service": "shop-api", "source": "203.0.113.0/24"},
        )
        assert outside.status_code == 403
        assert "10.0.0.0/8" in outside.json()["detail"]

        loopback = client.post(
            "/v1/services", json={"name": "local-only", "kind": "backend", "host": "127.0.0.1"}
        )
        assert loopback.status_code == 201
        shut = client.post("/v1/fleet/firewall/hub/open", json={"service": "local-only"})
        assert shut.status_code == 403
        assert "nothing to open" in shut.json()["detail"]


def test_a_fleet_wide_apply_will_not_give_up_its_rollback(settings: Settings):
    """One wrong rule would shut every machine at once. The window is the way back."""
    with TestClient(create_app(allowing(settings))) as client:
        refused = client.post("/v1/fleet/firewall/apply?rollback=0")
        assert refused.status_code == 400
        assert "shut every machine" in refused.json()["detail"]


def test_a_fleet_wide_apply_reports_a_line_for_every_node(settings: Settings):
    client = hub_with(allowing(settings), "build-01")
    try:
        body = client.post("/v1/fleet/firewall/apply?rollback=120").json()
        by_node = {one["node"]: one for one in body["results"]}
        assert set(by_node) == {"build-01", "hub"}
        # Nothing answered for build-01, and a node that was never reached has
        # already saved itself: its own watchdog is what puts it back.
        assert by_node["build-01"]["ok"] is False
    finally:
        client.__exit__(None, None, None)


def test_a_node_that_refuses_does_not_stop_the_others(settings: Settings):
    client = hub_with(allowing(settings), "db-03")
    try:
        body = client.post("/v1/fleet/firewall/apply?rollback=120").json()
        assert len(body["results"]) == 2
        assert [one["node"] for one in body["results"]] == ["db-03", "hub"]
    finally:
        client.__exit__(None, None, None)


def test_only_three_things_can_be_done_to_the_fleets_firewall(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        nonsense = client.post("/v1/fleet/firewall/burn")
        assert nonsense.status_code == 404
        assert "apply, confirm, restore" in nonsense.json()["detail"]


def test_the_fleet_apply_needs_the_switch_on_the_hub_as_well(settings: Settings):
    shut = allowing(settings, allow_remote_firewall=False)
    with TestClient(create_app(shut)) as client:
        body = client.post("/v1/fleet/firewall/apply?rollback=120").json()
        here = body["results"][0]
        assert here["node"] == "hub"
        assert here["ok"] is False
        assert "allow_remote_firewall" in here["detail"]


def test_confirming_across_a_fleet_needs_no_rollback_of_its_own(settings: Settings):
    """Only `apply` opens a window, so only `apply` has one to insist on."""
    with TestClient(create_app(allowing(settings))) as client:
        body = client.post("/v1/fleet/firewall/confirm").json()
        assert [one["node"] for one in body["results"]] == ["hub"]
        assert body["results"][0]["ok"] is False
        assert "nothing is waiting" in body["results"][0]["detail"]


def test_opening_everywhere_opens_on_the_node_that_holds_it(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        body = client.post("/v1/fleet/firewall/open", json={"service": "shop-api"}).json()
        assert [one["node"] for one in body["results"]] == ["hub"]
        assert body["results"][0]["ok"] is True
        assert "allow-shop-api" in body["results"][0]["detail"]


def test_a_node_that_does_not_hold_it_is_skipped_rather_than_failed(settings: Settings):
    client = hub_with(allowing(settings), "build-01")
    try:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        body = client.post("/v1/fleet/firewall/open", json={"service": "shop-api"}).json()
        by_node = {one["node"]: one for one in body["results"]}
        assert by_node["hub"]["ok"] is True
        assert by_node["build-01"]["detail"] == "does not hold shop-api"
    finally:
        client.__exit__(None, None, None)


def test_a_service_nobody_in_the_fleet_holds_is_a_404(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        missing = client.post("/v1/fleet/firewall/open", json={"service": "ghost"})
        assert missing.status_code == 404
        assert "no node in the fleet" in missing.json()["detail"]


def test_opening_everywhere_still_needs_the_switch_on_each_machine(settings: Settings):
    shut = allowing(settings, allow_remote_firewall=False)
    with TestClient(create_app(shut)) as client:
        client.post(
            "/v1/services",
            json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        )
        body = client.post("/v1/fleet/firewall/open", json={"service": "shop-api"}).json()
        assert body["results"][0]["ok"] is False
        assert "allow_remote_firewall" in body["results"][0]["detail"]
        assert client.get("/v1/firewall/rules").json() == []


def test_open_is_not_read_as_a_thing_to_do_to_a_firewall(settings: Settings):
    """`/firewall/open` and `/firewall/{what}` are two routes, and order decides."""
    with TestClient(create_app(allowing(settings))) as client:
        said = client.post("/v1/fleet/firewall/open", json={"service": "ghost"})
        assert said.status_code == 404
        assert "apply, confirm, restore" not in said.json()["detail"]


def test_a_rule_can_be_written_down_over_the_api(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        written = client.post("/v1/firewall/rules", json={"what": "ssh", "source": "10.0.0.0/8"})
        assert written.status_code == 200
        assert written.json()["name"] == "allow-ssh"
        assert written.json()["ports"] == [22]
        assert written.json()["origin"] == "catalogue"


def test_writing_a_rule_down_needs_the_switch(settings: Settings):
    """It is not bounded by the pool, which is exactly why the switch decides."""
    with TestClient(create_app(settings)) as client:
        refused = client.post("/v1/firewall/rules", json={"what": "ssh"})
        assert refused.status_code == 403
        assert "allow_remote_firewall" in refused.json()["detail"]


def test_a_rule_written_over_the_api_reads_the_same_words_as_the_command_line(
    settings: Settings,
):
    from warden.firewall import catalogue, model

    typed = catalogue.rule_for("ssh", action=model.Action.ALLOW, source="10.0.0.0/8")
    with TestClient(create_app(allowing(settings))) as client:
        asked = client.post(
            "/v1/firewall/rules", json={"what": "ssh", "source": "10.0.0.0/8"}
        ).json()
    assert asked["name"] == typed.name
    assert set(asked["ports"]) == typed.ports
    assert asked["protocol"] == typed.protocol


def test_a_comment_that_could_be_read_as_a_rule_is_refused_here_too(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        refused = client.post(
            "/v1/firewall/rules",
            json={"what": "ssh", "comment": "fine\ntcp dport 22 accept"},
        )
        assert refused.status_code == 422


def test_a_rule_can_be_written_down_on_a_node_by_name(settings: Settings):
    with TestClient(create_app(allowing(settings))) as client:
        written = client.post("/v1/fleet/firewall/hub/rules", json={"what": "https"})
        assert written.status_code == 200
        assert written.json()["node"] == "hub"
        assert written.json()["name"] == "allow-https"


def test_writing_on_a_node_by_name_needs_the_switch(settings: Settings):
    shut = allowing(settings, allow_remote_firewall=False)
    with TestClient(create_app(shut)) as client:
        refused = client.post("/v1/fleet/firewall/hub/rules", json={"what": "https"})
        assert refused.status_code == 403

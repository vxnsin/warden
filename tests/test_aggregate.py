import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from warden.errors import (
    NotPermittedError,
    RelayedError,
    UnknownNodeError,
    UnknownServiceError,
)
from warden.fleet import aggregate
from warden.models import Listener, Node, PoolStatus, Registration


def node(name: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        name=name,
        url=f"http://{name}:7010",
        pool_start=9000,
        pool_end=9099,
        version="0.1.0",
        first_seen=now,
        last_seen=now,
        expires_at=now + timedelta(seconds=90),
    )


def registration(name: str, port: int, **kwargs) -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind=kwargs.pop("kind", "backend"),
        project=kwargs.pop("project", None),
        host="127.0.0.1",
        port=port,
        pid=None,
        meta={},
        ttl=None,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )


def serving(**by_host: object) -> httpx.MockTransport:
    """A fleet where each host answers however the test says."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = by_host.get(request.url.host)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, json={"detail": "no"})
        payload = [item.model_dump(mode="json") for item in answer or []]
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def gather(nodes, local, transport, **kwargs):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.gather_services(
                http, nodes, here="hub", local=local, **kwargs
            )

    return asyncio.run(main())


def test_the_hubs_own_services_are_part_of_the_fleet():
    fleet = gather([], [registration("api", 8000)], serving())
    assert [(s.node, s.name) for s in fleet.services] == [("hub", "api")]
    assert fleet.unreachable == []


def test_every_node_is_asked_and_its_answers_tagged():
    fleet = gather(
        [node("build-01"), node("web-02")],
        [],
        serving(**{"build-01": [registration("runner", 9000)],
                   "web-02": [registration("site", 9000)]}),
    )
    assert [(s.node, s.name) for s in fleet.services] == [
        ("build-01", "runner"),
        ("web-02", "site"),
    ]


def test_services_are_grouped_by_node_then_port():
    fleet = gather(
        [node("web-02"), node("build-01")],
        [registration("hub-thing", 8000)],
        serving(**{"build-01": [registration("b", 9001), registration("a", 9000)],
                   "web-02": [registration("c", 9000)]}),
    )
    assert [(s.node, s.port) for s in fleet.services] == [
        ("build-01", 9000),
        ("build-01", 9001),
        ("hub", 8000),
        ("web-02", 9000),
    ]


def test_a_node_that_cannot_be_reached_is_named_not_dropped():
    fleet = gather(
        [node("build-01"), node("gone")],
        [],
        serving(**{"build-01": [registration("runner", 9000)],
                   "gone": httpx.ConnectError("nope")}),
    )
    assert [s.node for s in fleet.services] == ["build-01"]
    assert [(u.node, u.reason) for u in fleet.unreachable] == [
        ("gone", "could not be reached")
    ]


def test_one_dead_node_does_not_cost_the_others():
    fleet = gather(
        [node("a"), node("dead"), node("b")],
        [],
        serving(**{"a": [registration("x", 9000)],
                   "b": [registration("y", 9000)],
                   "dead": httpx.ConnectError("nope")}),
    )
    assert len(fleet.services) == 2
    assert len(fleet.unreachable) == 1


def test_a_refused_token_says_which_setting_to_look_at():
    fleet = gather([node("build-01")], [], serving(**{"build-01": 401}))
    assert "WARDEN_CLUSTER_TOKEN" in fleet.unreachable[0].reason


def test_another_error_from_a_node_is_reported_as_the_status():
    fleet = gather([node("build-01")], [], serving(**{"build-01": 500}))
    assert fleet.unreachable[0].reason == "answered 500"


def test_a_timeout_says_how_long_it_waited():
    assert "s" in aggregate.reason(httpx.TimeoutException("slow"))


def test_a_filter_is_passed_on_to_every_node():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[])

    gather([node("build-01")], [], httpx.MockTransport(handler), project="shop")
    assert seen == [{"project": "shop"}]


def test_a_name_only_one_node_holds_is_not_a_clash():
    fleet = gather(
        [node("build-01")],
        [registration("api", 8000)],
        serving(**{"build-01": [registration("runner", 9000)]}),
    )
    assert fleet.duplicates == []


def test_a_name_two_nodes_both_hold_is_named_with_its_nodes():
    fleet = gather(
        [node("build-01"), node("web-02")],
        [],
        serving(**{"build-01": [registration("shop-api", 9000)],
                   "web-02": [registration("shop-api", 9000)]}),
    )
    assert [(d.name, d.nodes) for d in fleet.duplicates] == [
        ("shop-api", ["build-01", "web-02"])
    ]


def test_the_hubs_own_services_count_towards_a_clash():
    fleet = gather(
        [node("build-01")],
        [registration("shop-api", 8000)],
        serving(**{"build-01": [registration("shop-api", 9000)]}),
    )
    assert [d.nodes for d in fleet.duplicates] == [["build-01", "hub"]]


def test_every_node_holding_the_name_is_listed_once_and_in_order():
    fleet = gather(
        [node("web-02"), node("build-01")],
        [registration("shop-api", 8000)],
        serving(**{"build-01": [registration("shop-api", 9000)],
                   "web-02": [registration("shop-api", 9000)]}),
    )
    assert [d.nodes for d in fleet.duplicates] == [["build-01", "hub", "web-02"]]


def test_clashes_are_only_reported_within_what_the_filter_showed():
    # A name hidden by --project is not in the table, so pointing at it would
    # send someone looking for a row that is not there.
    fleet = gather(
        [node("build-01")],
        [registration("shop-api", 8000, project="shop")],
        serving(**{"build-01": []}),
        project="shop",
    )
    assert fleet.duplicates == []


def look_up(nodes, transport, node_name, service):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.lookup_on(http, nodes, node_name, service)

    return asyncio.run(main())


def test_a_qualified_lookup_asks_the_named_node():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/services/runner"
        return httpx.Response(200, json=registration("runner", 9000).model_dump(mode="json"))

    found = look_up([node("build-01")], httpx.MockTransport(handler), "build-01", "runner")
    assert (found.node, found.name, found.port) == ("build-01", "runner", 9000)


def test_looking_up_on_a_node_nobody_knows():
    with pytest.raises(UnknownNodeError, match="no node registered"):
        look_up([], serving(), "build-01", "runner")


def test_a_service_missing_on_that_node_says_which_node():
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"detail": "no"}))
    with pytest.raises(UnknownServiceError, match="on 'build-01'"):
        look_up([node("build-01")], transport, "build-01", "runner")


def test_a_node_that_will_not_answer_a_lookup_says_so():
    transport = httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("nope", request=request))
    )
    with pytest.raises(UnknownNodeError, match="could not be reached"):
        look_up([node("build-01")], transport, "build-01", "runner")


def test_the_cluster_token_is_what_the_hub_carries():
    assert aggregate.client("secret").headers["Authorization"] == "Bearer secret"
    assert "Authorization" not in aggregate.client(None).headers


def pool(allocated: int = 0, *, start: int = 9000, end: int = 9099, reserved=()) -> PoolStatus:
    usable = (end - start + 1) - len(reserved)
    return PoolStatus(
        start=start,
        end=end,
        size=end - start + 1,
        reserved=list(reserved),
        allocated=allocated,
        available=usable - allocated,
    )


def pooling(**by_host: object) -> httpx.MockTransport:
    """A fleet where each host reports the pool the test gives it."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = by_host.get(request.url.host)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, json={"detail": "no"})
        return httpx.Response(200, json=answer.model_dump(mode="json"))

    return httpx.MockTransport(handler)


def gather_pools(nodes, local, transport):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.gather_pools(http, nodes, here="hub", local=local)

    return asyncio.run(main())


def test_the_hubs_own_pool_is_part_of_the_fleet():
    fleet = gather_pools([], pool(3, start=8000, end=8999), pooling())
    assert [(p.node, p.allocated) for p in fleet.pools] == [("hub", 3)]
    assert fleet.unreachable == []


def test_every_nodes_pool_is_asked_for_and_sorted_by_node():
    fleet = gather_pools(
        [node("web-02"), node("build-01")],
        pool(1, start=8000, end=8999),
        pooling(**{"build-01": pool(2), "web-02": pool(4)}),
    )
    assert [p.node for p in fleet.pools] == ["build-01", "hub", "web-02"]


def test_the_totals_add_the_nodes_up():
    fleet = gather_pools(
        [node("build-01"), node("web-02")],
        pool(1, start=8000, end=8099),
        pooling(**{"build-01": pool(2), "web-02": pool(4)}),
    )
    assert fleet.allocated == 7
    assert fleet.available == (100 - 1) + (100 - 2) + (100 - 4)
    assert fleet.capacity == 300


def test_reserved_ports_are_not_capacity_anyone_can_have():
    fleet = gather_pools([], pool(0, start=8000, end=8009, reserved=(8005, 8006)), pooling())
    assert fleet.capacity == 8
    assert fleet.available == 8


def test_a_node_that_cannot_be_reached_is_named_and_not_counted():
    fleet = gather_pools(
        [node("build-01"), node("gone")],
        pool(1, start=8000, end=8099),
        pooling(**{"build-01": pool(2), "gone": httpx.ConnectError("nope")}),
    )
    assert [p.node for p in fleet.pools] == ["build-01", "hub"]
    assert [(u.node, u.reason) for u in fleet.unreachable] == [("gone", "could not be reached")]
    assert fleet.capacity == 200


def relayed(handler):
    async def main(call):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await call(http)

    return main


def answering(status: int, body: object, *, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.host, request.url.path, request.read()))
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    return handler


def test_registering_goes_to_the_named_node_untouched():
    seen: list = []
    payload = {"name": "runner", "kind": "worker"}
    run = relayed(answering(201, registration("runner", 9000).model_dump(mode="json"), seen=seen))
    service, created = asyncio.run(
        run(lambda http: aggregate.register_on(http, [node("build-01")], "build-01", payload))
    )
    assert (service.node, service.port, created) == ("build-01", 9000, True)
    assert seen[0][:3] == ("POST", "build-01", "/v1/services")
    assert json.loads(seen[0][3]) == payload


def test_a_renewed_registration_is_not_reported_as_new():
    run = relayed(answering(200, registration("runner", 9000).model_dump(mode="json")))
    _, created = asyncio.run(
        run(lambda http: aggregate.register_on(http, [node("build-01")], "build-01", {}))
    )
    assert created is False


def test_a_refusal_from_the_node_arrives_in_the_nodes_own_words():
    # The node is the machine that can try to bind the port, so its answer is
    # the answer. Reworded here, nobody could tell which warden refused.
    run = relayed(answering(409, {"detail": "port 3000 is held by 'legacy-crm'"}))
    with pytest.raises(RelayedError) as caught:
        asyncio.run(
            run(lambda http: aggregate.register_on(http, [node("build-01")], "build-01", {}))
        )
    assert caught.value.message == "port 3000 is held by 'legacy-crm'"
    assert caught.value.status_code == 409


def test_a_full_pool_on_the_node_stays_a_full_pool_here():
    run = relayed(answering(503, {"detail": "no free port left in 9000-9099 on 127.0.0.1"}))
    with pytest.raises(RelayedError) as caught:
        asyncio.run(
            run(lambda http: aggregate.register_on(http, [node("build-01")], "build-01", {}))
        )
    assert caught.value.status_code == 503


def test_releasing_asks_the_node_that_handed_the_port_out():
    seen: list = []
    run = relayed(answering(204, None, seen=seen))
    asyncio.run(run(lambda http: aggregate.release_on(http, [node("build-01")], "build-01", "x")))
    assert seen[0][:3] == ("DELETE", "build-01", "/v1/services/x")


def test_a_heartbeat_is_carried_to_the_node_and_the_answer_tagged():
    seen: list = []
    run = relayed(answering(200, registration("runner", 9000).model_dump(mode="json"), seen=seen))
    service = asyncio.run(
        run(
            lambda http: aggregate.heartbeat_on(
                http, [node("build-01")], "build-01", "runner", {"ttl": 60}
            )
        )
    )
    assert seen[0][:3] == ("POST", "build-01", "/v1/services/runner/heartbeat")
    assert service.node == "build-01"


def test_relaying_to_a_node_nobody_knows_never_leaves_the_hub():
    seen: list = []
    run = relayed(answering(201, {}, seen=seen))
    with pytest.raises(UnknownNodeError, match="no node registered"):
        asyncio.run(run(lambda http: aggregate.release_on(http, [], "build-01", "x")))
    assert seen == []


def test_a_node_that_will_not_answer_a_relay_says_which_node():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    run = relayed(handler)
    with pytest.raises(UnknownNodeError, match="build-01 could not be reached"):
        asyncio.run(
            run(lambda http: aggregate.release_on(http, [node("build-01")], "build-01", "x"))
        )


def test_the_hub_forwards_the_callers_own_credentials_and_none_of_its_own():
    assert aggregate.relaying("Bearer mine").headers["Authorization"] == "Bearer mine"
    assert "Authorization" not in aggregate.relaying(None).headers


def socket(port: int, **kwargs) -> Listener:
    return Listener(
        protocol=kwargs.pop("protocol", "tcp"),
        host="127.0.0.1",
        port=port,
        pid=kwargs.pop("pid", 4242),
        process=kwargs.pop("process", "python.exe"),
        user=None,
        started_at=None,
        command=None,
    )


def gather_listeners(nodes, local, transport):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.gather_listeners(http, nodes, here="hub", local=local)

    return asyncio.run(main())


def test_this_machines_own_sockets_are_part_of_the_fleet():
    found = gather_listeners([], [socket(8000)], serving())
    assert [(item.node, item.port) for item in found.listeners] == [("hub", 8000)]


def test_every_nodes_sockets_are_tagged_with_the_machine_they_are_on():
    found = gather_listeners(
        [node("build-01")],
        [socket(3000)],
        serving(**{"build-01": [socket(3000)]}),
    )
    # The same number twice, and the node is the only thing telling them apart.
    assert [(item.node, item.port) for item in found.listeners] == [
        ("build-01", 3000),
        ("hub", 3000),
    ]


def test_sockets_are_grouped_by_node_then_port_then_protocol():
    found = gather_listeners(
        [node("build-01")],
        [],
        serving(**{"build-01": [socket(9001), socket(9000, protocol="udp"),
                                socket(9000)]}),
    )
    assert [(item.port, item.protocol) for item in found.listeners] == [
        (9000, "tcp"),
        (9000, "udp"),
        (9001, "tcp"),
    ]


def test_a_machine_that_cannot_be_reached_is_named_not_dropped_from_the_ports():
    found = gather_listeners(
        [node("gone")], [socket(8000)], serving(**{"gone": httpx.ConnectError("nope")})
    )
    assert [item.node for item in found.listeners] == ["hub"]
    assert [item.node for item in found.unreachable] == ["gone"]


def test_stopping_a_process_is_asked_of_the_machine_it_runs_on():
    seen: list = []
    run = relayed(answering(204, None, seen=seen))
    asyncio.run(run(lambda http: aggregate.stop_on(http, [node("build-01")], "build-01", 99)))
    assert seen[0][:3] == ("DELETE", "build-01", "/v1/listeners/99")


def test_a_node_with_killing_switched_off_refuses_in_its_own_words():
    run = relayed(answering(403, {"detail": "stopping processes over the API is switched off"}))
    with pytest.raises(RelayedError) as caught:
        asyncio.run(run(lambda http: aggregate.stop_on(http, [node("build-01")], "build-01", 99)))
    assert caught.value.status_code == 403
    assert "switched off" in caught.value.message


def relay_to(nodes, transport, *, require_https=False):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await aggregate.release_on(
                http, nodes, "build-01", "api", require_https=require_https
            )

    return asyncio.run(main())


def secure_node(name: str = "build-01") -> Node:
    return node(name).model_copy(update={"url": f"https://{name}:7010"})


def test_a_token_is_not_sent_to_a_plain_http_node_when_https_is_required():
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    with pytest.raises(NotPermittedError, match="in the clear"):
        relay_to([node("build-01")], transport, require_https=True)


def test_https_nodes_are_relayed_to_as_usual():
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    assert relay_to([secure_node()], transport, require_https=True) is None


def test_plain_http_still_works_when_nothing_asked_otherwise():
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    assert relay_to([node("build-01")], transport) is None


def test_plain_http_is_named_in_the_log_once_per_node(caplog):
    aggregate._warned.clear()
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    with caplog.at_level("WARNING", logger="warden.fleet"):
        relay_to([node("build-01")], transport)
        relay_to([node("build-01")], transport)
    assert sum("plain HTTP" in record.message for record in caplog.records) == 1


def test_an_https_node_is_never_warned_about(caplog):
    aggregate._warned.clear()
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    with caplog.at_level("WARNING", logger="warden.fleet"):
        relay_to([secure_node()], transport)
    assert not caplog.records

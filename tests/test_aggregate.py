import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from warden import aggregate
from warden.errors import UnknownNodeError, UnknownServiceError
from warden.models import Node, Registration


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

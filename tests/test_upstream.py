import asyncio
import json

import httpx
import pytest

from warden import __version__
from warden.config import Settings
from warden.upstream import UpstreamReporter


@pytest.fixture
def edge() -> Settings:
    return Settings(
        node="build-01",
        upstream="http://hub:7010",
        advertise="http://build-01:7010",
        pool_start=9000,
        pool_end=9099,
        cluster_token="between-wardens",
    )


def test_the_announcement_says_who_and_what(edge: Settings):
    reporter = UpstreamReporter(edge)
    assert reporter.announcement == {
        "name": "build-01",
        "url": "http://build-01:7010",
        "pool_start": 9000,
        "pool_end": 9099,
        "version": __version__,
    }


def test_reporting_is_more_frequent_than_the_lease(edge: Settings):
    assert UpstreamReporter(edge).interval < edge.node_ttl


def test_a_very_short_lease_does_not_turn_into_a_hot_loop():
    reporter = UpstreamReporter(Settings(node_ttl=10, upstream="http://hub:7010",
                                         advertise="http://build-01:7010"))
    assert reporter.interval >= 5


def test_the_cluster_token_is_what_goes_on_the_wire(edge: Settings):
    client = UpstreamReporter(edge).client()
    assert client.headers["Authorization"] == "Bearer between-wardens"
    assert str(client.base_url) == "http://hub:7010"


def test_a_warden_without_a_cluster_token_sends_none():
    settings = Settings(upstream="http://hub:7010", advertise="http://build-01:7010")
    assert "Authorization" not in UpstreamReporter(settings).client().headers


def test_the_hub_receives_the_announcement(edge: Settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={})

    async def scenario() -> bool:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as http:
            return await UpstreamReporter(edge).announce_once(http)

    assert asyncio.run(scenario()) is True
    assert seen == [("/v1/nodes", UpstreamReporter(edge).announcement)]


def test_a_hub_that_is_away_does_not_raise(edge: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    async def scenario() -> bool:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as http:
            return await UpstreamReporter(edge).announce_once(http)

    assert asyncio.run(scenario()) is False


def test_a_hub_that_refuses_the_token_does_not_raise_either(edge: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid or missing cluster token"})

    async def scenario() -> bool:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as http:
            return await UpstreamReporter(edge).announce_once(http)

    assert asyncio.run(scenario()) is False


def test_a_lone_warden_starts_no_reporter():
    reporter = UpstreamReporter(Settings())

    async def scenario() -> None:
        reporter.start()
        assert reporter._task is None
        await reporter.stop()

    asyncio.run(scenario())


def test_the_reporter_stops_cleanly(edge: Settings):
    reporter = UpstreamReporter(edge)

    async def scenario() -> None:
        reporter.start()
        assert reporter._task is not None
        await asyncio.sleep(0)
        await reporter.stop()
        assert reporter._task is None

    asyncio.run(scenario())

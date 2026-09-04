import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from warden import cli
from warden.api import create_app
from warden.cli import app
from warden.core import events as bus_module
from warden.core.config import Settings
from warden.core.events import EventBus, redacted
from warden.core.store import EXPIRED, REGISTERED, RELEASED, RENEWED, Store
from warden.models import Event, RegistrationRequest
from warden.ports.service import Registry

runner_cli = CliRunner()


def request(name: str, **kwargs) -> RegistrationRequest:
    return RegistrationRequest(name=name, kind=kwargs.pop("kind", "backend"), **kwargs)


def an_event(action: str = REGISTERED, name: str = "api") -> Event:
    return Event(
        at=datetime.now(UTC),
        action=action,
        name=name,
        kind="backend",
        project=None,
        host="127.0.0.1",
        port=8000,
        pid=None,
    )


async def until(condition, timeout: float = 2.0) -> bool:
    """Give the loop room to do it, rather than guessing how long it takes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.005)
    return True


def test_a_committed_change_reaches_a_listener(manager: Registry, store: Store):
    seen: list[Event] = []
    store.subscribe(seen.append)
    manager.register(request("api"))
    assert [event.action for event in seen] == [REGISTERED]
    assert seen[0].name == "api"
    assert seen[0].port == 8000


def test_every_kind_of_change_is_announced(manager: Registry, store: Store):
    seen: list[Event] = []
    store.subscribe(seen.append)
    manager.register(request("api"))
    manager.register(request("api"))
    manager.release("api")
    manager.register(request("worker", ttl=1))
    store.purge_expired(datetime.now(UTC) + timedelta(hours=1))
    assert [event.action for event in seen] == [
        REGISTERED,
        RENEWED,
        RELEASED,
        REGISTERED,
        EXPIRED,
    ]


def test_a_listener_that_breaks_does_not_take_the_registration_with_it(
    manager: Registry, store: Store
):
    def unhappy(event: Event) -> None:
        raise RuntimeError("no")

    seen: list[Event] = []
    store.subscribe(unhappy)
    store.subscribe(seen.append)
    service = manager.register(request("api"))[0]
    assert service.port == 8000
    assert manager.history()[0].action == REGISTERED
    assert len(seen) == 1


def test_nothing_is_announced_before_it_is_committed(manager: Registry, store: Store):
    """A listener must never see a change the next reader cannot."""
    during: list[list[str]] = []
    store.subscribe(lambda event: during.append([s.name for s in manager.list()]))
    manager.register(request("api"))
    assert during == [["api"]]


def test_a_watcher_hears_what_happens(settings: Settings):
    async def scenario() -> list[str]:
        bus = EventBus(settings)
        bus.start()
        async with bus.watch() as queue:
            bus.publish(an_event())
            bus.publish(an_event(RELEASED))
            await until(lambda: queue.qsize() == 2)
            await bus.stop()
            return [queue.get_nowait().action for _ in range(queue.qsize())]

    assert asyncio.run(scenario()) == [REGISTERED, RELEASED]


def test_a_reader_that_falls_behind_loses_the_oldest_and_not_the_newest(settings: Settings):
    async def scenario() -> tuple[str, int]:
        bus = EventBus(settings)
        bus.start()
        async with bus.watch() as queue:
            for index in range(bus_module.WATCHING + 5):
                bus.publish(an_event(name=f"api-{index}"))
            await until(lambda: bus.status.dropped >= 5)
            last = None
            while not queue.empty():
                last = queue.get_nowait()
            await bus.stop()
            assert last is not None
            return last.name, bus.status.dropped

    name, dropped = asyncio.run(scenario())
    assert name == f"api-{bus_module.WATCHING + 4}"
    assert dropped >= 5


def test_a_bus_nobody_started_does_not_blow_up_on_a_write():
    EventBus(Settings()).publish(an_event())


def posting(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Send every webhook into a transport the test can look at."""
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def fake(**kwargs) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(bus_module.httpx, "AsyncClient", fake)
    return sent


def hooked(**overrides) -> Settings:
    return Settings(
        node="build-01",
        webhook="https://chat.example/services/T0/B0/xxxx",
        update_check=False,
        **overrides,
    )


def test_an_event_is_posted_where_it_was_told_to(monkeypatch: pytest.MonkeyPatch):
    sent = posting(monkeypatch, lambda request: httpx.Response(204))

    async def scenario() -> EventBus:
        bus = EventBus(hooked())
        bus.start()
        bus.publish(an_event())
        await until(lambda: bool(sent))
        await bus.stop()
        return bus

    bus = asyncio.run(scenario())
    assert len(sent) == 1
    assert str(sent[0].url) == "https://chat.example/services/T0/B0/xxxx"
    assert sent[0].headers["X-Warden-Event"] == REGISTERED
    assert bus.status.delivered == 1


def test_a_renewal_is_not_worth_telling_a_chat_channel(monkeypatch: pytest.MonkeyPatch):
    sent = posting(monkeypatch, lambda request: httpx.Response(204))

    async def scenario() -> None:
        bus = EventBus(hooked())
        bus.start()
        bus.publish(an_event(RENEWED))
        bus.publish(an_event(RELEASED))
        await until(lambda: bool(sent))
        await bus.stop()

    asyncio.run(scenario())
    assert [request.headers["X-Warden-Event"] for request in sent] == [RELEASED]


def test_asking_for_renewals_gets_them(monkeypatch: pytest.MonkeyPatch):
    sent = posting(monkeypatch, lambda request: httpx.Response(204))

    async def scenario() -> None:
        bus = EventBus(hooked(webhook_events="renewed"))
        bus.start()
        bus.publish(an_event(RENEWED))
        await until(lambda: bool(sent))
        await bus.stop()

    asyncio.run(scenario())
    assert [request.headers["X-Warden-Event"] for request in sent] == [RENEWED]


def test_a_receiver_that_is_down_is_retried_and_then_reported(monkeypatch: pytest.MonkeyPatch):
    sent = posting(monkeypatch, lambda request: httpx.Response(500))
    monkeypatch.setattr(bus_module, "BACKOFF", 0.0)

    async def scenario() -> EventBus:
        bus = EventBus(hooked())
        bus.start()
        bus.publish(an_event())
        await until(lambda: bus.status.failed == 1)
        await bus.stop()
        return bus

    bus = asyncio.run(scenario())
    assert len(sent) == bus_module.ATTEMPTS
    assert bus.status.failed == 1
    assert bus.status.last_error


def test_a_receiver_that_comes_back_clears_the_complaint(monkeypatch: pytest.MonkeyPatch):
    answers = [httpx.Response(500), httpx.Response(204)]
    posting(monkeypatch, lambda request: answers.pop(0))
    monkeypatch.setattr(bus_module, "BACKOFF", 0.0)

    async def scenario() -> EventBus:
        bus = EventBus(hooked())
        bus.start()
        bus.publish(an_event())
        await until(lambda: bus.status.delivered == 1)
        await bus.stop()
        return bus

    bus = asyncio.run(scenario())
    assert bus.status.delivered == 1
    assert bus.status.last_error is None


def test_the_status_never_repeats_the_secret_half_of_the_address():
    status = EventBus(hooked()).status
    assert status.configured
    assert status.target == "https://chat.example/..."
    assert "xxxx" not in str(status.model_dump())


def test_an_address_without_a_path_keeps_its_host():
    assert redacted("http://hooks.example") == "http://hooks.example"
    assert redacted(None) is None


def test_a_warden_with_nowhere_to_post_says_so():
    status = EventBus(Settings()).status
    assert not status.configured
    assert status.target is None
    assert status.format is None


def _every_route(router):
    """Every route on an app, including the ones behind an included router."""
    for route in getattr(router, "routes", []):
        yield route
        yield from _every_route(route)
        yield from _every_route(getattr(route, "original_router", None))


def _stream_endpoint(settings: Settings):
    return next(
        route.endpoint
        for route in _every_route(create_app(settings))
        if getattr(route, "path", None) == "/v1/events"
    )


def frames_of(settings: Settings, published: list[Event]) -> list[str]:
    """What the endpoint actually writes down the wire.

    Driven directly rather than through a test client, because a stream that
    never ends is exactly what a test client waits for.
    """
    endpoint = _stream_endpoint(settings)

    async def scenario() -> list[str]:
        bus = EventBus(settings)
        bus.start()
        response = await endpoint(bus)
        frames = [await response.body_iterator.__anext__()]
        for event in published:
            bus.publish(event)
            frames.append(await response.body_iterator.__anext__())
        await response.body_iterator.aclose()
        await bus.stop()
        return frames

    return asyncio.run(scenario())


def test_the_stream_says_hello_before_anything_has_happened(settings: Settings):
    assert frames_of(settings, [])[0].startswith(":")


def test_an_event_is_framed_the_way_a_browser_expects(settings: Settings):
    frame = frames_of(settings, [an_event()])[1]
    name, data = frame.rstrip("\n").split("\n")
    assert name == f"event: {REGISTERED}"
    assert json.loads(data.removeprefix("data: "))["name"] == "api"
    assert frame.endswith("\n\n")


def test_the_stream_is_served_as_events_and_not_as_a_page(settings: Settings):
    async def scenario() -> str:
        bus = EventBus(settings)
        bus.start()
        response = await _stream_endpoint(settings)(bus)
        await response.body_iterator.aclose()
        await bus.stop()
        return response.media_type

    assert asyncio.run(scenario()) == "text/event-stream"


def test_a_warden_with_nowhere_to_post_says_so_over_the_api(settings: Settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/webhook").json() == {
            "configured": False,
            "target": None,
            "format": None,
            "actions": [],
            "watching": 0,
            "delivered": 0,
            "failed": 0,
            "dropped": 0,
            "last_error": None,
            "last_sent": None,
        }


def test_where_events_go_is_only_told_to_a_caller_with_the_token(settings: Settings):
    guarded = settings.model_copy(update={"token": "secret"})
    with TestClient(create_app(guarded)) as client:
        assert client.get("/v1/webhook").status_code == 401
        assert client.get("/v1/events").status_code == 401
        allowed = client.get("/v1/webhook", headers={"Authorization": "Bearer secret"})
        assert allowed.status_code == 200


class FakeStream:
    def __init__(self, events: list[Event]) -> None:
        self.given = events

    def events(self):
        yield from self.given

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_the_command_prints_a_line_per_event(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        cli, "_client", lambda url, token: FakeStream([an_event(), an_event(RELEASED)])
    )
    result = runner_cli.invoke(app, ["events"])
    assert result.exit_code == 0
    assert REGISTERED in result.stdout
    assert RELEASED in result.stdout
    assert "127.0.0.1:8000" in result.stdout


def test_the_stream_can_be_piped_somewhere(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "_client", lambda url, token: FakeStream([an_event()]))
    result = runner_cli.invoke(app, ["events", "--json"])
    written = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [event["action"] for event in written] == [REGISTERED]

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from warden import cli
from warden.allocator import PortPool
from warden.api import create_app
from warden.cli import app
from warden.config import Settings
from warden.models import Event, RegistrationRequest
from warden.service import Registry
from warden.store import EXPIRED, MOVED, REGISTERED, RELEASED, RENEWED, Store

runner_cli = CliRunner()


def request(name: str, **kwargs) -> RegistrationRequest:
    return RegistrationRequest(name=name, kind=kwargs.pop("kind", "backend"), **kwargs)


def actions(events: list[Event]) -> list[str]:
    return [event.action for event in events]


def test_registering_is_written_down(manager: Registry):
    manager.register(request("api"))
    events = manager.history()
    assert actions(events) == [REGISTERED]
    assert events[0].name == "api"
    assert events[0].port == 8000


def test_registering_again_on_the_same_port_is_a_renewal(manager: Registry):
    manager.register(request("api"))
    manager.register(request("api"))
    assert actions(manager.history()) == [RENEWED, REGISTERED]


def test_a_service_that_changes_port_is_written_down_as_moved(manager: Registry):
    manager.register(request("api"))
    manager.register(request("api", require_port=8003))
    assert manager.history()[0].action == MOVED
    assert manager.history()[0].port == 8003


def test_releasing_is_written_down_with_the_port_it_gave_back(manager: Registry):
    manager.register(request("api"))
    manager.release("api")
    assert actions(manager.history()) == [RELEASED, REGISTERED]
    assert manager.history()[0].port == 8000


def test_a_lease_that_runs_out_is_written_down_too(manager: Registry):
    manager.register(request("api", ttl=1))
    manager.store.purge_expired(datetime.now(UTC) + timedelta(seconds=2))
    assert actions(manager.history()) == [EXPIRED, REGISTERED]


def test_history_is_newest_first(manager: Registry):
    manager.register(request("api"))
    manager.register(request("web", kind="frontend"))
    assert [event.name for event in manager.history()] == ["web", "api"]


def test_one_port_can_be_asked_about_on_its_own(manager: Registry):
    manager.register(request("api"))
    manager.register(request("web", kind="frontend"))
    assert [event.name for event in manager.history(port=8000)] == ["api"]


def test_one_service_can_be_asked_about_on_its_own(manager: Registry):
    manager.register(request("api"))
    manager.register(request("web", kind="frontend"))
    assert [event.port for event in manager.history(name="web")] == [8001]


def test_what_a_port_used_to_hold_outlives_the_registration(manager: Registry):
    # The whole point: the registry forgets, this does not.
    manager.register(request("api"))
    manager.release("api")
    manager.register(request("web", kind="frontend"))
    assert [event.name for event in manager.history(port=8000)] == ["web", "api", "api"]


def test_the_number_kept_is_capped_so_a_long_run_stays_small():
    with Store(":memory:", event_cap=5) as store:
        registry = Registry(store, PortPool(8000, 8004), probe=False)
        for _ in range(20):
            registry.register(request("api"))
        assert len(store.history(limit=100)) == 5


def test_the_ones_kept_are_the_newest():
    with Store(":memory:", event_cap=3) as store:
        registry = Registry(store, PortPool(8000, 8004), probe=False)
        registry.register(request("api"))
        for name in ("one", "two", "three"):
            registry.register(request(name, kind="worker"))
        assert [event.name for event in store.history()] == ["three", "two", "one"]


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def test_the_api_hands_back_what_happened(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    body = client.get("/v1/history").json()
    assert [event["action"] for event in body] == [REGISTERED]


def test_the_api_can_be_asked_about_one_port(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    client.post("/v1/services", json={"name": "web", "kind": "frontend"})
    body = client.get("/v1/history", params={"port": 8001}).json()
    assert [event["name"] for event in body] == ["web"]


def test_the_api_refuses_a_limit_that_would_return_the_whole_database(client: TestClient):
    assert client.get("/v1/history", params={"limit": 10_000}).status_code == 422


class FakeClient:
    def __init__(self, events: list[Event]) -> None:
        self.events = events
        self.asked: dict[str, object] = {}

    def history(self, *, port=None, name=None, limit=100) -> list[Event]:
        self.asked = {"port": port, "name": name, "limit": limit}
        return self.events

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def event(action: str = REGISTERED, name: str = "api", port: int = 8000) -> Event:
    return Event(
        at=datetime.now(UTC),
        action=action,
        name=name,
        kind="backend",
        project=None,
        host="127.0.0.1",
        port=port,
        pid=4242,
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient([event(RELEASED), event(REGISTERED)])
    monkeypatch.setattr(cli, "_client", lambda url, token: fake)
    return fake


def test_a_number_is_read_as_a_port(recorded: FakeClient):
    runner_cli.invoke(app, ["history", "8000"])
    assert recorded.asked["port"] == 8000
    assert recorded.asked["name"] is None


def test_anything_else_is_read_as_a_service_name(recorded: FakeClient):
    runner_cli.invoke(app, ["history", "shop-api"])
    assert recorded.asked["name"] == "shop-api"
    assert recorded.asked["port"] is None


def test_without_an_argument_it_shows_everything_recent(recorded: FakeClient):
    result = runner_cli.invoke(app, ["history"])
    assert result.exit_code == 0
    assert recorded.asked == {"port": None, "name": None, "limit": 20}


def test_every_event_is_shown_with_what_happened(recorded: FakeClient):
    result = runner_cli.invoke(app, ["history"])
    assert RELEASED in result.output
    assert REGISTERED in result.output
    assert "127.0.0.1:8000" in result.output


def test_nothing_recorded_says_so_rather_than_printing_an_empty_table(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cli, "_client", lambda url, token: FakeClient([]))
    result = runner_cli.invoke(app, ["history", "8000"])
    assert "nothing recorded for 8000" in result.output

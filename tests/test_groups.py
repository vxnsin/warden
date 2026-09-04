from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from warden import cli
from warden.allocator import PortPool
from warden.api import create_app
from warden.cli import app
from warden.config import Settings
from warden.errors import PoolExhaustedError
from warden.models import GroupRequest, Registration, RegistrationRequest
from warden.service import Registry
from warden.store import Store

runner_cli = CliRunner()


def group(name: str = "stack", **kwargs) -> GroupRequest:
    return GroupRequest(name=name, kind=kwargs.pop("kind", "backend"), **kwargs)


def one(name: str, **kwargs) -> RegistrationRequest:
    return RegistrationRequest(name=name, kind=kwargs.pop("kind", "backend"), **kwargs)


def ports_of(services: list[Registration]) -> list[int]:
    return [service.port for service in services]


def test_a_group_is_named_and_numbered(manager: Registry):
    held = manager.register_group(group(count=3))
    assert [service.name for service in held] == ["stack-1", "stack-2", "stack-3"]
    assert ports_of(held) == [8000, 8001, 8002]


def test_asking_twice_does_not_shuffle_a_running_stack(manager: Registry):
    first = manager.register_group(group(count=3))
    again = manager.register_group(group(count=3))
    assert ports_of(again) == ports_of(first)


def test_a_group_leaves_room_for_what_is_already_there(manager: Registry):
    manager.register(one("api", require_port=8001))
    assert ports_of(manager.register_group(group(count=2))) == [8000, 8002]


def test_a_contiguous_group_comes_back_in_a_row(manager: Registry):
    manager.register(one("api", require_port=8000))
    held = manager.register_group(group(count=3, contiguous=True))
    assert ports_of(held) == [8001, 8002, 8003]


def test_a_run_that_is_not_there_is_refused_rather_than_scattered(manager: Registry):
    manager.register(one("api", require_port=8001))
    manager.register(one("web", require_port=8003))
    with pytest.raises(PoolExhaustedError) as refused:
        manager.register_group(group(count=2, contiguous=True))
    assert "no run of 2 free ports" in str(refused.value)


def test_a_refused_group_leaves_nothing_behind(manager: Registry):
    manager.register(one("api", require_port=8001))
    manager.register(one("web", require_port=8003))
    with pytest.raises(PoolExhaustedError):
        manager.register_group(group(count=2, contiguous=True))
    assert [service.name for service in manager.list()] == ["api", "web"]


def test_more_ports_than_there_are_is_refused_before_anything_is_written(manager: Registry):
    with pytest.raises(PoolExhaustedError) as refused:
        manager.register_group(group(count=9))
    assert "only 5 free ports" in str(refused.value)
    assert manager.list() == []


def test_a_group_is_written_whole_or_not_at_all(store: Store, monkeypatch: pytest.MonkeyPatch):
    manager = Registry(store, PortPool(8000, 8004), probe=False)
    written = store._write
    calls = {"n": 0}

    def falter(registration: Registration) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("the disk had other ideas")
        written(registration)

    monkeypatch.setattr(store, "_write", falter)
    with pytest.raises(RuntimeError):
        manager.register_group(group(count=4))
    assert manager.list() == []


def test_a_group_that_moves_does_not_trip_over_its_own_ports(manager: Registry):
    manager.register_group(group(count=2))
    manager.register(one("api", require_port=8004))
    moved = manager.register_group(group(count=3, contiguous=True))
    assert ports_of(moved) == [8000, 8001, 8002]


def test_a_name_too_long_to_number_is_refused():
    with pytest.raises(ValueError, match="too long a name"):
        GroupRequest(name="a" * 63, kind="backend", count=10)


def test_the_pool_says_how_long_a_run_it_still_has(manager: Registry):
    assert manager.pool_status().largest_run == 5
    manager.register(one("api", require_port=8002))
    status = manager.pool_status()
    assert status.available == 4
    assert status.largest_run == 2


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as client:
        yield client


def test_the_api_hands_out_a_whole_group(client: TestClient):
    response = client.post("/v1/groups", json={"name": "stack", "kind": "backend", "count": 3})
    assert response.status_code == 201
    assert [service["name"] for service in response.json()] == [
        "stack-1",
        "stack-2",
        "stack-3",
    ]


def test_the_api_turns_down_a_group_the_pool_cannot_serve(client: TestClient):
    response = client.post("/v1/groups", json={"name": "stack", "kind": "backend", "count": 9})
    assert response.status_code == 503
    assert client.get("/v1/services").json() == []


def test_the_api_refuses_a_group_larger_than_any_stack_needs(client: TestClient):
    response = client.post("/v1/groups", json={"name": "stack", "kind": "backend", "count": 999})
    assert response.status_code == 422


class FakeClient:
    def __init__(self) -> None:
        self.asked: dict[str, object] = {}

    def register_group(self, name: str, **kwargs) -> list[Registration]:
        self.asked = {"name": name, **kwargs}
        return [
            Registration(
                name=f"{name}-{index}",
                kind="backend",
                project=None,
                host="127.0.0.1",
                port=8000 + index - 1,
                pid=None,
                meta={},
                ttl=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                expires_at=None,
            )
            for index in range(1, kwargs["count"] + 1)
        ]

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_the_command_prints_one_port_per_line(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "_client", lambda url, token: FakeClient())
    result = runner_cli.invoke(app, ["register", "stack", "--kind", "backend", "--count", "3"])
    assert result.exit_code == 0
    assert result.stdout.split() == ["8000", "8001", "8002"]


def test_the_command_passes_contiguous_on(monkeypatch: pytest.MonkeyPatch):
    fake = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda url, token: fake)
    runner_cli.invoke(
        app, ["register", "stack", "--kind", "backend", "--count", "2", "--contiguous"]
    )
    assert fake.asked["contiguous"] is True


def test_a_wish_for_one_port_and_a_request_for_several_do_not_go_together(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cli, "_client", lambda url, token: FakeClient())
    result = runner_cli.invoke(
        app,
        ["register", "stack", "--kind", "backend", "--count", "2", "--require-port", "8000"],
    )
    assert result.exit_code == 1
    assert "do not go together" in result.stderr

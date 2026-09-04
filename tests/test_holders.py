from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psutil
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from warden import cli
from warden.api import create_app
from warden.cli import app
from warden.core.config import Settings
from warden.errors import UnknownServiceError
from warden.models import Registration, RegistrationRequest
from warden.ports.listeners import GONE, RUNNING, SETTLING, holding
from warden.ports.service import Registry

runner_cli = CliRunner()


def unused_pid() -> int:
    taken = set(psutil.pids())
    candidate = max(taken) + 1000
    while candidate in taken or psutil.pid_exists(candidate):
        candidate += 1
    return candidate


def registration(name: str, port: int, *, pid: int | None = None, age: float = 0.0):
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind="backend",
        project=None,
        host="127.0.0.1",
        port=port,
        pid=pid,
        meta={},
        ttl=None,
        created_at=now,
        updated_at=now - timedelta(seconds=age),
        expires_at=None,
    )


def test_a_registration_made_a_moment_ago_is_never_called_gone():
    # It has a port and has not bound it yet. That is starting, not dead.
    status, reason = holding(8000, unused_pid(), since=1.0, bound=set())
    assert (status, reason) == (RUNNING, None)


def test_a_port_nothing_listens_on_is_gone():
    status, reason = holding(8000, None, since=SETTLING + 1, bound=set())
    assert status == GONE
    assert reason == "nothing is listening on 8000"


def test_a_dead_process_on_a_silent_port_names_both():
    pid = unused_pid()
    status, reason = holding(8001, pid, since=SETTLING + 1, bound=set())
    assert status == GONE
    assert reason == f"nothing is on 8001 and pid {pid} is gone"


def test_a_dead_process_whose_port_is_now_somebody_elses_says_so():
    pid = unused_pid()
    status, reason = holding(8001, pid, since=SETTLING + 1, bound={8001})
    assert status == GONE
    assert reason == f"pid {pid} is gone and 8001 is held by something else"


def test_a_live_process_on_a_bound_port_is_running():
    import os

    status, reason = holding(8002, os.getpid(), since=SETTLING + 1, bound={8002})
    assert (status, reason) == (RUNNING, None)


@pytest.mark.sockets
def test_filling_in_holders_leaves_the_registrations_otherwise_alone(manager: Registry):
    manager.register(RegistrationRequest(name="api", kind="backend"))
    filled = manager.with_holders(manager.list())
    assert [service.name for service in filled] == ["api"]
    assert filled[0].holder in {RUNNING, GONE}


def test_a_holder_is_only_worked_out_when_it_was_asked_for(manager: Registry):
    manager.register(RegistrationRequest(name="api", kind="backend"))
    assert manager.list()[0].holder is None


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def test_the_listing_says_nothing_about_holders_by_default(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    assert client.get("/v1/services").json()[0]["holder"] is None


@pytest.mark.sockets
def test_the_listing_can_be_asked_for_holders(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    body = client.get("/v1/services", params={"holders": True}).json()
    assert body[0]["holder"] in {RUNNING, GONE}


class FakeClient:
    def __init__(self, services: list[Registration]) -> None:
        self._services = services
        self.released: list[str] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def services(self, *, project=None, kind=None, holders=False) -> list[Registration]:
        return self._services

    def release(self, name: str, *, node=None) -> None:
        if name not in {service.name for service in self._services}:
            raise UnknownServiceError(f"no service registered as {name!r}")
        self.released.append(name)


@pytest.fixture
def stale(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    services = [
        registration("api", 8000).model_copy(update={"holder": RUNNING}),
        registration("old-job", 8001, pid=9930).model_copy(
            update={"holder": GONE, "holder_reason": "nothing is on 8001 and pid 9930 is gone"}
        ),
    ]
    fake = FakeClient(services)
    monkeypatch.setattr(cli, "_client", lambda url, token: fake)
    return fake


def test_the_listing_can_be_narrowed_to_the_ones_that_are_gone(stale: FakeClient):
    result = runner_cli.invoke(app, ["ls", "--stale"])
    assert result.exit_code == 0
    assert "old-job" in result.output
    assert "api" not in result.output


def test_holders_cannot_be_asked_of_the_whole_fleet():
    # Only the machine a service runs on can see whether it is still there.
    result = runner_cli.invoke(app, ["ls", "--holders", "--all"])
    assert result.exit_code == 1


def test_reaping_asks_before_it_releases_anything(stale: FakeClient):
    result = runner_cli.invoke(app, ["reap"], input="n\n")
    assert result.exit_code == 0
    assert "pid 9930 is gone" in result.output
    assert stale.released == []


def test_reaping_releases_the_one_that_is_gone_and_nothing_else(stale: FakeClient):
    result = runner_cli.invoke(app, ["reap"], input="y\n")
    assert result.exit_code == 0
    assert stale.released == ["old-job"]


def test_reaping_can_skip_the_questions(stale: FakeClient):
    result = runner_cli.invoke(app, ["reap", "--yes"])
    assert result.exit_code == 0
    assert stale.released == ["old-job"]


def test_reaping_with_nothing_to_reap_says_so(monkeypatch: pytest.MonkeyPatch):
    fake = FakeClient([registration("api", 8000).model_copy(update={"holder": RUNNING})])
    monkeypatch.setattr(cli, "_client", lambda url, token: fake)
    result = runner_cli.invoke(app, ["reap", "--yes"])
    assert result.exit_code == 0
    assert "still there" in result.output
    assert fake.released == []

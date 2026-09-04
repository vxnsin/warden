import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from warden import __version__, cli, health
from warden.cli import app
from warden.config import Settings
from warden.errors import UnknownServiceError, WardenError
from warden.health import FAIL, NOTE, OK, WARN, Check, examine, exit_code
from warden.listeners import GONE, RUNNING
from warden.models import (
    Health,
    Node,
    PoolStatus,
    Registration,
    UpdateStatus,
    WebhookStatus,
)

runner_cli = CliRunner()


def registration(name: str, port: int, holder: str = RUNNING) -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind="backend",
        project=None,
        host="127.0.0.1",
        port=port,
        pid=None,
        meta={},
        ttl=None,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        holder=holder,
    )


def node(name: str, *, online: bool = True) -> Node:
    now = datetime.now(UTC)
    return Node(
        name=name,
        url=f"http://{name}:7010",
        pool_start=8000,
        pool_end=8999,
        version=__version__,
        first_seen=now,
        last_seen=now,
        expires_at=now + timedelta(seconds=90) if online else now - timedelta(seconds=90),
    )


def health_of(*, version: str = __version__, nodes: int = 0) -> Health:
    return Health(
        status="ok", version=version, node="hub", role="hub", services=1, nodes=nodes
    )


class FakeClient:
    def __init__(self, **overrides: object) -> None:
        self.url = "http://127.0.0.1:7010"
        self.answers: dict[str, object] = {
            "health": health_of(),
            "pool": PoolStatus(
                start=8000, end=8999, size=1000, reserved=[], allocated=1, available=999
            ),
            "services": [registration("api", 8000)],
            "nodes": [],
            "update_status": UpdateStatus(current=__version__, available=False),
            "webhook": WebhookStatus(configured=False),
        }
        self.answers.update(overrides)

    def _answer(self, name: str):
        answer = self.answers[name]
        if isinstance(answer, WardenError):
            raise answer
        return answer

    def health(self):
        return self._answer("health")

    def pool(self):
        return self._answer("pool")

    def services(self, *, project=None, kind=None, holders=False):
        return self._answer("services")

    def nodes(self):
        return self._answer("nodes")

    def update_status(self):
        return self._answer("update_status")

    def webhook(self):
        return self._answer("webhook")

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def levels(checks: list[Check]) -> list[str]:
    return [check.level for check in checks]


def text_of(checks: list[Check], level: str) -> str:
    return " ".join(check.text for check in checks if check.level == level)


def test_a_healthy_warden_reports_nothing_worse_than_ok(settings: Settings):
    checks = examine(FakeClient(), settings)
    assert set(levels(checks)) <= {OK, NOTE}
    assert exit_code(checks) == 0


def test_a_warden_that_is_not_there_fails_and_stops_asking(settings: Settings):
    client = FakeClient(health=WardenError("no warden reachable at http://127.0.0.1:7010"))
    checks = examine(client, settings)
    assert checks[0].level == FAIL
    assert exit_code(checks) == 1
    # Nothing after it would have anything to say.
    assert "pool" not in text_of(checks, OK)


def test_a_settings_line_is_shown_even_when_nothing_answers(settings: Settings):
    client = FakeClient(health=WardenError("no warden reachable"))
    assert any("settings from" in check.text for check in examine(client, settings))


def test_an_open_warden_without_a_token_is_a_warning(tmp_path):
    settings = Settings(database=tmp_path / "r.db", host="0.0.0.0", update_check=False)
    checks = examine(FakeClient(), settings)
    assert WARN in levels(checks)
    assert "no token" in text_of(checks, WARN)
    # A warning is not a failure: a health check must not call this machine down.
    assert exit_code(checks) == 0


def test_an_open_warden_with_a_token_is_fine(tmp_path):
    settings = Settings(
        database=tmp_path / "r.db", host="0.0.0.0", token="secret", update_check=False
    )
    assert WARN not in levels(examine(FakeClient(), settings))


def test_an_exhausted_pool_is_a_failure(settings: Settings):
    client = FakeClient(
        pool=PoolStatus(start=8000, end=8004, size=5, reserved=[], allocated=5, available=0)
    )
    checks = examine(client, settings)
    assert FAIL in levels(checks)
    assert exit_code(checks) == 1


def test_a_nearly_empty_pool_is_only_a_warning(settings: Settings):
    client = FakeClient(
        pool=PoolStatus(start=8000, end=8009, size=10, reserved=[], allocated=9, available=1)
    )
    checks = examine(client, settings)
    assert WARN in levels(checks)
    assert exit_code(checks) == 0


def test_registrations_that_are_gone_are_pointed_at_reap(settings: Settings):
    client = FakeClient(services=[registration("old", 8001, holder=GONE)])
    assert "warden reap" in text_of(examine(client, settings), WARN)


def test_a_stale_node_is_a_warning(settings: Settings):
    client = FakeClient(health=health_of(nodes=1), nodes=[node("build-01", online=False)])
    assert "build-01 stale" in text_of(examine(client, settings), WARN)


def test_an_online_node_is_named_with_when_it_was_last_seen(settings: Settings):
    client = FakeClient(health=health_of(nodes=1), nodes=[node("build-01")])
    assert "build-01 online, last seen" in text_of(examine(client, settings), OK)


def test_a_newer_version_is_a_warning(settings: Settings):
    client = FakeClient(
        update_status=UpdateStatus(current=__version__, latest="9.9.9", available=True)
    )
    assert "9.9.9 is out" in text_of(examine(client, settings), WARN)


def test_a_check_that_could_not_run_is_only_a_note(settings: Settings):
    client = FakeClient(
        update_status=UpdateStatus(current=__version__, reason="github is unreachable")
    )
    checks = examine(client, settings)
    assert "github is unreachable" in text_of(checks, NOTE)
    assert exit_code(checks) == 0


def test_two_versions_in_one_place_are_worth_saying(settings: Settings):
    client = FakeClient(health=health_of(version="0.0.1"))
    assert "this command is" in text_of(examine(client, settings), NOTE)


def test_a_listing_that_cannot_be_read_fails_rather_than_being_ignored(settings: Settings):
    client = FakeClient(services=UnknownServiceError("gone"))
    assert exit_code(examine(client, settings)) == 1


def test_the_command_prints_a_line_per_check(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "_client", lambda url, token: FakeClient())
    result = runner_cli.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "answering at" in result.output


def test_the_command_exits_one_when_something_failed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        cli, "_client", lambda url, token: FakeClient(health=WardenError("nothing there"))
    )
    assert runner_cli.invoke(app, ["doctor"]).exit_code == 1


def test_the_report_can_be_read_by_a_machine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "_client", lambda url, token: FakeClient())
    payload = json.loads(runner_cli.invoke(app, ["doctor", "--json"]).output)
    assert {check["level"] for check in payload} <= {OK, NOTE, WARN, FAIL}


def edge(tmp_path) -> Settings:
    return Settings(
        database=tmp_path / "r.db",
        node="build-01",
        upstream="http://hub:7010",
        advertise="http://build-01:7010",
        cluster_token="fleet",
        update_check=False,
    )


def test_an_edge_says_which_hub_it_reports_to(tmp_path, monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def answering(url, timeout=None, headers=None):
        seen["url"] = url
        seen["headers"] = headers
        return httpx.Response(
            200,
            json=health_of().model_dump(mode="json"),
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(health.httpx, "get", answering)
    checks = examine(FakeClient(), edge(tmp_path))
    assert "reporting to http://hub:7010 as build-01" in text_of(checks, OK)
    assert seen["url"] == "http://hub:7010/health"
    assert seen["headers"] == {"Authorization": "Bearer fleet"}


def test_a_hub_that_cannot_be_reached_is_a_failure(tmp_path, monkeypatch: pytest.MonkeyPatch):
    def refused(url, timeout=None, headers=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(health.httpx, "get", refused)
    checks = examine(FakeClient(), edge(tmp_path))
    assert "cannot reach the warden at http://hub:7010" in text_of(checks, FAIL)
    assert exit_code(checks) == 1

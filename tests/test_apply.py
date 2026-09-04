import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from warden import cli
from warden.api import create_app
from warden.cli import app
from warden.client import WardenClient
from warden.config import Settings

runner_cli = CliRunner()

MANIFEST = """
[project]
name = "shop"

[services.api]
kind = "backend"

[services.worker]
kind = "worker"
"""


class Borrowed(WardenClient):
    """The real client, talking to the app in this process."""

    def __init__(self, served: TestClient) -> None:
        self._http = served

    def close(self) -> None:
        return None


@pytest.fixture
def project(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    with TestClient(create_app(settings)) as served:
        monkeypatch.setattr(cli, "_client", lambda url, token: Borrowed(served))
        monkeypatch.chdir(tmp_path)
        Path("warden.toml").write_text(MANIFEST, encoding="utf-8")
        yield tmp_path


def applied(*args: str) -> dict:
    result = runner_cli.invoke(app, ["apply", "--json", *args], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_applying_registers_everything_the_file_asks_for(project: Path):
    done = applied()
    assert done["project"] == "shop"
    assert [service["name"] for service in done["services"]] == ["shop-api", "shop-worker"]
    assert [service["what"] for service in done["services"]] == ["taken", "taken"]


def test_applying_twice_changes_nothing_the_second_time(project: Path):
    first = applied()
    again = applied()
    assert [s["address"] for s in again["services"]] == [s["address"] for s in first["services"]]
    assert [s["what"] for s in again["services"]] == ["renewed", "renewed"]


def test_the_env_file_is_written_where_it_was_asked_for(project: Path):
    done = applied("--env", ".env")
    text = (project / ".env").read_text(encoding="utf-8")
    port = done["services"][0]["address"].split(":")[1]
    assert f"SHOP_API_PORT={port}" in text
    assert text.splitlines()[0].startswith("# Written by `warden apply`")


def test_the_table_says_what_it_did(project: Path):
    result = runner_cli.invoke(app, ["apply"])
    assert result.exit_code == 0
    assert "shop-api" in result.stdout
    assert "taken" in result.stdout


def test_a_port_that_cannot_be_had_leaves_nothing_registered(project: Path):
    runner_cli.invoke(
        app, ["register", "squatter", "--kind", "backend", "--require-port", "8002"]
    )
    Path("warden.toml").write_text(
        '[project]\nname = "shop"\n\n'
        '[services.api]\nkind = "backend"\n\n'
        '[services.db]\nkind = "database"\nrequire_port = 8002\n',
        encoding="utf-8",
    )
    result = runner_cli.invoke(app, ["apply"])
    assert result.exit_code == 1
    assert "8002" in result.stderr
    listed = json.loads(runner_cli.invoke(app, ["ls", "--json"]).stdout)
    assert [service["name"] for service in listed] == ["squatter"]

def test_releasing_gives_the_project_its_ports_back(project: Path):
    applied()
    done = applied("--release")
    assert [service["what"] for service in done["services"]] == ["released", "released"]
    assert runner_cli.invoke(app, ["ls", "--json"]).stdout.strip() == "[]"


def test_releasing_something_that_was_never_registered_is_not_an_error(project: Path):
    done = applied("--release")
    assert [service["what"] for service in done["services"]] == ["gone", "gone"]


def test_a_manifest_that_is_not_there_is_said_plainly(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    result = runner_cli.invoke(app, ["apply"])
    assert result.exit_code == 1
    assert "write one, or say --file" in result.stderr

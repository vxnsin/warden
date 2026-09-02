import json

import pytest
from typer.testing import CliRunner

from warden import __version__, cli, theme
from warden.cli import app
from warden.models import FleetListener, FleetListeners

runner = CliRunner()


def socket_on(node: str, port: int) -> FleetListener:
    return FleetListener(
        node=node,
        protocol="tcp",
        host="127.0.0.1",
        port=port,
        pid=4242,
        process="python.exe",
        user=None,
        started_at=None,
        command=None,
    )


@pytest.fixture
def fleet_ports(monkeypatch: pytest.MonkeyPatch) -> FleetListeners:
    found = FleetListeners(
        listeners=[socket_on("build-01", 9000), socket_on("hub", 3000)], unreachable=[]
    )
    monkeypatch.setattr(cli, "_fleet_ports", lambda url, token, *, udp: (found, {}))
    return found


def test_the_bare_command_introduces_itself():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert theme.TAGLINE in result.output
    assert "Usage" in result.output


def test_the_bare_command_still_lists_what_it_can_do():
    result = runner.invoke(app, [])
    for command in ("serve", "tui", "ls", "register", "release"):
        assert command in result.output


def test_the_version_is_printed_on_its_own():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__
    assert theme.TAGLINE not in result.output


def test_a_port_filter_narrows_the_fleet_table(fleet_ports: FleetListeners):
    result = runner.invoke(app, ["ports", "--all", "--port", "3000"])
    assert result.exit_code == 0
    assert "3000" in result.output
    assert "9000" not in result.output


def test_a_port_filter_narrows_the_json_of_a_fleet_listing_too(fleet_ports: FleetListeners):
    # The table and --json answer the same question, so they must not disagree
    # about which rows the filter left.
    result = runner.invoke(app, ["ports", "--all", "--port", "3000", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert [item["port"] for item in body["listeners"]] == [3000]


def test_without_a_port_filter_the_whole_fleet_is_dumped(fleet_ports: FleetListeners):
    result = runner.invoke(app, ["ports", "--all", "--json"])
    body = json.loads(result.output)
    assert [item["port"] for item in body["listeners"]] == [9000, 3000]

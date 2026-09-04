import json
import pkgutil
import sys

import pytest
from typer.testing import CliRunner

from warden import __version__, theme
from warden.cli import app
from warden.cli.commands import machine
from warden.models import FleetListener, FleetListeners

runner_cli = CliRunner()


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
    monkeypatch.setattr(machine, "_fleet_ports", lambda url, token, *, udp: (found, {}))
    return found


def test_the_bare_command_introduces_itself():
    result = runner_cli.invoke(app, [])
    assert result.exit_code == 0
    assert theme.TAGLINE in result.output
    assert "Usage" in result.output


def test_the_bare_command_still_lists_what_it_can_do():
    result = runner_cli.invoke(app, [])
    for command in ("serve", "tui", "ls", "register", "release"):
        assert command in result.output


def test_the_version_is_printed_on_its_own():
    result = runner_cli.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__
    assert theme.TAGLINE not in result.output


def test_a_port_filter_narrows_the_fleet_table(fleet_ports: FleetListeners):
    result = runner_cli.invoke(app, ["ports", "--all", "--port", "3000"])
    assert result.exit_code == 0
    assert "3000" in result.output
    assert "9000" not in result.output


def test_a_port_filter_narrows_the_json_of_a_fleet_listing_too(fleet_ports: FleetListeners):
    # The table and --json answer the same question, so they must not disagree
    # about which rows the filter left.
    result = runner_cli.invoke(app, ["ports", "--all", "--port", "3000", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert [item["port"] for item in body["listeners"]] == [3000]


def test_without_a_port_filter_the_whole_fleet_is_dumped(fleet_ports: FleetListeners):
    result = runner_cli.invoke(app, ["ports", "--all", "--json"])
    body = json.loads(result.output)
    assert [item["port"] for item in body["listeners"]] == [9000, 3000]


def test_run_needs_something_to_run():
    result = runner_cli.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "after `--`" in result.output


def test_run_refuses_when_no_warden_answers(monkeypatch):
    monkeypatch.setenv("WARDEN_URL", "http://127.0.0.1:9")
    result = runner_cli.invoke(app, ["run", "--", sys.executable, "-c", "pass"])
    assert result.exit_code == 1
    assert "--anyway" in result.output


def test_run_says_nothing_is_registered_when_it_carries_on_anyway(monkeypatch):
    monkeypatch.setenv("WARDEN_URL", "http://127.0.0.1:9")
    result = runner_cli.invoke(
        app, ["run", "--anyway", "--", sys.executable, "-c", "pass"]
    )
    assert result.exit_code == 0
    assert "unregistered" in result.output


def test_every_command_module_is_found_without_being_listed():
    """A group of commands is a file in the folder and nothing else."""
    from warden.cli import commands

    commands.load()
    found = {module.name for module in pkgutil.iter_modules(commands.__path__)}
    assert {"admin", "registry", "firewall", "fleet", "machine"} <= found


def test_the_help_reads_in_the_order_the_modules_ask_for():
    """Alphabetical is not how somebody meets a tool for the first time."""
    from warden.cli import commands
    from warden.cli.shared import app as loaded

    commands.load()
    order = [command.callback.__module__ for command in loaded.registered_commands]
    seen = list(dict.fromkeys(order))
    assert seen.index("warden.cli.commands.admin") < seen.index("warden.cli.commands.registry")
    assert seen.index("warden.cli.commands.registry") < seen.index("warden.cli.commands.fleet")


def test_the_command_line_still_answers_for_everything_it_used_to():
    from warden.cli.shared import app as loaded

    names = {command.name or command.callback.__name__ for command in loaded.registered_commands}
    assert {"register", "ls", "ports", "doctor", "apply", "export", "events"} <= names
    groups = {group.name for group in loaded.registered_groups}
    assert groups == {"settings", "service", "firewall"}

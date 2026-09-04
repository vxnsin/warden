import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from warden.cli import app
from warden.core import autostart
from warden.core.autostart import (
    INSTALLED,
    MISSING,
    RUNNING,
    Launchd,
    Plan,
    StartupCommand,
    Systemd,
    autostart_for,
    serve_command,
)
from warden.errors import NotPermittedError, WardenError

runner_cli = CliRunner()


class Ran:
    """Stands in for the service manager, and remembers what it was told."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.commands: list[list[str]] = []

    def __call__(self, command, capture_output=True, text=True, check=False):
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "")

    @property
    def flat(self) -> str:
        return " | ".join(" ".join(command) for command in self.commands)


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> Ran:
    fake = Ran()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_the_command_it_installs_is_the_one_on_the_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/local/bin/warden")
    assert serve_command() == ["/usr/local/bin/warden", "serve"]


def test_without_one_on_the_path_it_falls_back_to_this_interpreter(
    monkeypatch: pytest.MonkeyPatch,
):
    # Installed into a virtual environment that is not on PATH, which is how
    # anyone trying it out for the first time has it.
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    assert serve_command() == [sys.executable, "-m", "warden", "serve"]


def test_each_system_gets_its_own_way_of_starting_things():
    assert isinstance(autostart_for("Linux"), Systemd)
    assert isinstance(autostart_for("Darwin"), Launchd)
    assert isinstance(autostart_for("Windows"), StartupCommand)


def test_a_system_it_does_not_know_is_told_so_plainly():
    with pytest.raises(NotPermittedError, match="does not know how"):
        autostart_for("Haiku")


@pytest.fixture
def systemd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Systemd:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return Systemd()


def test_the_unit_starts_warden_and_comes_back_after_a_crash(systemd: Systemd):
    unit = systemd.unit()
    assert "ExecStart=" in unit
    assert "serve" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_installing_writes_the_unit_and_enables_it(systemd: Systemd, ran: Ran):
    systemd.install()
    assert systemd.path.is_file()
    assert "daemon-reload" in ran.flat
    assert "enable --now warden.service" in ran.flat


def test_uninstalling_stops_it_before_the_file_goes(systemd: Systemd, ran: Ran):
    systemd.install()
    systemd.uninstall()
    assert not systemd.path.exists()
    assert ran.commands[-2] == ["systemctl", "--user", "disable", "--now", "warden.service"]


def test_a_unit_that_is_not_there_is_not_installed(systemd: Systemd):
    assert systemd.status() == MISSING


def test_an_enabled_unit_that_is_up_is_running(
    systemd: Systemd, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(subprocess, "run", Ran(stdout="active\n"))
    systemd.path.parent.mkdir(parents=True, exist_ok=True)
    systemd.path.write_text(systemd.unit(), encoding="utf-8")
    assert systemd.status() == RUNNING


def test_an_enabled_unit_that_is_down_is_only_installed(
    systemd: Systemd, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(subprocess, "run", Ran(stdout="inactive\n"))
    systemd.path.parent.mkdir(parents=True, exist_ok=True)
    systemd.path.write_text(systemd.unit(), encoding="utf-8")
    assert systemd.status() == INSTALLED


def test_a_service_manager_that_refuses_says_why(
    systemd: Systemd, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(subprocess, "run", Ran(returncode=1, stdout="Failed to connect"))
    with pytest.raises(WardenError, match="Failed to connect"):
        systemd.install()


@pytest.fixture
def launchd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Launchd:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return Launchd()


def test_the_agent_names_itself_and_the_command(launchd: Launchd):
    agent = launchd.agent()
    assert autostart.LABEL in agent
    assert "<key>RunAtLoad</key>" in agent
    assert "<string>serve</string>" in agent


def test_installing_the_agent_writes_it_and_loads_it(launchd: Launchd, ran: Ran):
    launchd.install()
    assert launchd.path.is_file()
    assert "launchctl load -w" in ran.flat


def test_removing_the_agent_unloads_it_first(launchd: Launchd, ran: Ran):
    launchd.install()
    launchd.uninstall()
    assert not launchd.path.exists()
    assert "unload -w" in ran.flat


@pytest.fixture
def startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StartupCommand:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return StartupCommand()


def test_the_startup_command_starts_warden_out_of_the_way(startup: StartupCommand):
    assert startup.script().startswith("@echo off")
    assert '/min' in startup.script()
    assert startup.script().endswith("\r\n")


def test_installing_needs_nothing_but_a_file(startup: StartupCommand, ran: Ran):
    # Nothing is asked of a service manager, so nothing can refuse it.
    startup.install()
    assert startup.path.is_file()
    assert ran.commands == []


def test_removing_it_takes_the_file_away(startup: StartupCommand):
    startup.install()
    startup.uninstall()
    assert startup.status() == MISSING


def test_removing_one_that_was_never_there_is_not_an_error(startup: StartupCommand):
    startup.uninstall()
    assert startup.status() == MISSING


def test_without_a_warden_on_the_port_it_is_installed_but_not_running(
    startup: StartupCommand, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(autostart, "is_bound", lambda host, port: False)
    startup.install()
    assert startup.status() == INSTALLED


def test_with_one_answering_it_is_running(
    startup: StartupCommand, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(autostart, "is_bound", lambda host, port: True)
    startup.install()
    assert startup.status() == RUNNING


class FakeStarter:
    kind = "test autostart"

    def __init__(self, state: str = MISSING) -> None:
        self.state = state
        self.installed = False
        self.removed = False
        self.remarks: list[str] = []

    def plan(self) -> Plan:
        return Plan(kind=self.kind, path=Path("/tmp/warden.service"), body="body", steps=["go"])

    def install(self) -> None:
        self.installed = True
        self.state = INSTALLED

    def uninstall(self) -> None:
        self.removed = True
        self.state = MISSING

    def status(self) -> str:
        return self.state

    def notes(self) -> list[str]:
        return self.remarks


@pytest.fixture
def starter(monkeypatch: pytest.MonkeyPatch) -> FakeStarter:
    fake = FakeStarter()
    monkeypatch.setattr(autostart, "autostart_for", lambda system=None: fake)
    return fake


def test_installing_shows_everything_it_would_do_before_doing_it(starter: FakeStarter):
    result = runner_cli.invoke(app, ["service", "install"], input="n\n")
    assert result.exit_code == 0
    assert "warden.service" in result.output
    assert "body" in result.output
    assert "go" in result.output
    assert starter.installed is False


def test_saying_yes_installs_it(starter: FakeStarter):
    result = runner_cli.invoke(app, ["service", "install"], input="y\n")
    assert result.exit_code == 0
    assert starter.installed is True


def test_it_can_be_installed_without_being_asked(starter: FakeStarter):
    assert runner_cli.invoke(app, ["service", "install", "--yes"]).exit_code == 0
    assert starter.installed is True


def test_removing_something_that_is_not_there_says_so(starter: FakeStarter):
    result = runner_cli.invoke(app, ["service", "uninstall", "--yes"])
    assert "does not start at login" in result.output
    assert starter.removed is False


def test_removing_asks_first(starter: FakeStarter):
    starter.state = INSTALLED
    runner_cli.invoke(app, ["service", "uninstall"], input="n\n")
    assert starter.removed is False


def test_removing_takes_it_away_when_confirmed(starter: FakeStarter):
    starter.state = INSTALLED
    runner_cli.invoke(app, ["service", "uninstall"], input="y\n")
    assert starter.removed is True


def test_the_status_names_the_kind_it_found(starter: FakeStarter):
    starter.state = RUNNING
    result = runner_cli.invoke(app, ["service", "status"])
    assert RUNNING in result.output
    assert "test autostart" in result.output


def test_a_system_without_a_way_to_do_this_fails_rather_than_pretending(
    monkeypatch: pytest.MonkeyPatch,
):
    def refuse(system=None):
        raise NotPermittedError("warden does not know how to start itself at login on Haiku")

    monkeypatch.setattr(autostart, "autostart_for", refuse)
    assert runner_cli.invoke(app, ["service", "status"]).exit_code == 1


def test_a_unit_that_will_not_survive_a_logout_is_said_out_loud(
    monkeypatch: pytest.MonkeyPatch,
):
    """Installed and working are not the same thing on a server."""
    unit = Systemd()
    monkeypatch.setattr(unit, "lingering", lambda: False)
    assert "enable-linger" in " ".join(unit.notes())
    assert "log out" in " ".join(unit.notes())


def test_a_lingering_account_is_not_nagged(monkeypatch: pytest.MonkeyPatch):
    unit = Systemd()
    monkeypatch.setattr(unit, "lingering", lambda: True)
    assert unit.notes() == []


def test_nothing_is_claimed_when_nothing_can_answer(monkeypatch: pytest.MonkeyPatch):
    unit = Systemd()
    monkeypatch.setattr(unit, "lingering", lambda: None)
    assert unit.notes() == []


def test_lingering_reads_what_loginctl_says(monkeypatch: pytest.MonkeyPatch):
    for said, expected in {"Linger=yes": True, "Linger=no": False}.items():
        monkeypatch.setattr(
            autostart,
            "_run",
            lambda command, said=said: subprocess.CompletedProcess(command, 0, said, ""),
        )
        assert Systemd().lingering() is expected


def test_a_machine_without_loginctl_is_not_guessed_about(monkeypatch: pytest.MonkeyPatch):
    def missing(command):
        raise WardenError("could not run loginctl")

    monkeypatch.setattr(autostart, "_run", missing)
    assert Systemd().lingering() is None


def test_the_note_reaches_the_command_line(starter: FakeStarter):
    starter.remarks = ["this account does not linger"]
    result = runner_cli.invoke(app, ["service", "status"])
    assert "does not linger" in result.stderr

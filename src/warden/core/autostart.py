"""Making `warden serve` survive a reboot, on whichever system this is.

Always as the user who runs the command, never as root or SYSTEM: a warden
started by another account would read another account's settings and hand out
ports from a registry nobody can see.
"""

from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from warden.core.config import Settings
from warden.errors import NotPermittedError, WardenError
from warden.ports.allocator import is_bound

LABEL = "com.github.vxnsin.warden"

INSTALLED = "installed"
RUNNING = "running"
MISSING = "not installed"


def serve_command() -> list[str]:
    """How to start a warden from here, however this one was installed."""
    executable = shutil.which("warden")
    if executable:
        return [executable, "serve"]
    return [sys.executable, "-m", "warden", "serve"]


def _quoted(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise WardenError(f"could not run {command[0]} - {exc}") from exc


def _must(command: list[str], doing: str) -> None:
    result = _run(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise WardenError(f"{doing} failed: {detail}")


@dataclass(frozen=True)
class Plan:
    """What installing would do, printed before anything is written."""

    kind: str
    path: Path | None
    body: str | None
    steps: list[str]


class Autostart:
    """One platform's way of starting something at login."""

    kind = "autostart"

    def plan(self) -> Plan:
        raise NotImplementedError

    def install(self) -> None:
        raise NotImplementedError

    def uninstall(self) -> None:
        raise NotImplementedError

    def status(self) -> str:
        raise NotImplementedError

    def notes(self) -> list[str]:
        """What is true of this machine that the plan alone does not say."""
        return []


class Systemd(Autostart):
    kind = "systemd user unit"

    @property
    def path(self) -> Path:
        root = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        return Path(root) / "systemd" / "user" / "warden.service"

    def unit(self) -> str:
        return (
            "[Unit]\n"
            "Description=warden - hands out ports and says who holds them\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            f"ExecStart={_quoted(serve_command())}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def plan(self) -> Plan:
        return Plan(
            kind=self.kind,
            path=self.path,
            body=self.unit(),
            steps=[
                "systemctl --user daemon-reload",
                "systemctl --user enable --now warden.service",
            ],
        )

    def install(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.unit(), encoding="utf-8")
        _must(["systemctl", "--user", "daemon-reload"], "reloading systemd")
        _must(
            ["systemctl", "--user", "enable", "--now", "warden.service"],
            "enabling the unit",
        )

    def uninstall(self) -> None:
        # Stopped before the file goes, or systemd is left holding a unit that
        # no longer exists anywhere on disk.
        _run(["systemctl", "--user", "disable", "--now", "warden.service"])
        self.path.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])

    def status(self) -> str:
        if not self.path.is_file():
            return MISSING
        active = _run(["systemctl", "--user", "is-active", "warden.service"])
        return RUNNING if active.stdout.strip() == "active" else INSTALLED

    def lingering(self) -> bool | None:
        """Whether this account's services outlive its last session.

        None when nothing can say - a container without logind, where the
        question does not arise in the same shape.
        """
        try:
            result = _run(
                ["loginctl", "show-user", getpass.getuser(), "--property=Linger"]
            )
        except WardenError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip().endswith("=yes")

    def notes(self) -> list[str]:
        """The one thing that makes this look installed on a server and not be.

        A user unit belongs to the user's session. Without lingering it stops
        when the last one ends, which on a server is the moment you log out of
        ssh - long after `warden service install` said it worked.
        """
        if self.lingering() is not False:
            return []
        user = getpass.getuser()
        return [
            "this account does not linger, so the unit stops when its last session "
            f"ends - on a server, when you log out. `sudo loginctl enable-linger {user}` "
            "keeps it running."
        ]


class Launchd(Autostart):
    kind = "launchd agent"

    @property
    def path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def agent(self) -> str:
        arguments = "\n".join(f"        <string>{part}</string>" for part in serve_command())
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "    <key>Label</key>\n"
            f"    <string>{LABEL}</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"{arguments}\n"
            "    </array>\n"
            "    <key>RunAtLoad</key>\n"
            "    <true/>\n"
            "    <key>KeepAlive</key>\n"
            "    <dict>\n"
            "        <key>SuccessfulExit</key>\n"
            "        <false/>\n"
            "    </dict>\n"
            "</dict>\n"
            "</plist>\n"
        )

    def plan(self) -> Plan:
        return Plan(
            kind=self.kind,
            path=self.path,
            body=self.agent(),
            steps=[f"launchctl load -w {self.path}"],
        )

    def install(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.agent(), encoding="utf-8")
        _run(["launchctl", "unload", str(self.path)])
        _must(["launchctl", "load", "-w", str(self.path)], "loading the agent")

    def uninstall(self) -> None:
        _run(["launchctl", "unload", "-w", str(self.path)])
        self.path.unlink(missing_ok=True)

    def status(self) -> str:
        if not self.path.is_file():
            return MISSING
        listed = _run(["launchctl", "list"])
        return RUNNING if LABEL in listed.stdout else INSTALLED


class StartupCommand(Autostart):
    """A command in the Startup folder, which needs no administrator.

    Windows also has scheduled tasks, but creating one is refused outright on
    plenty of managed machines - and a tool that only installs itself where the
    user is an administrator is no use on the machines that most need it.
    """

    kind = "startup command"

    @property
    def path(self) -> Path:
        roaming = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return (
            Path(roaming)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / "warden.cmd"
        )

    def script(self) -> str:
        # Minimised, because this runs at every logon and a console window
        # taking the foreground would be its own reason to uninstall it.
        return '@echo off\r\nstart "" /min ' + _quoted(serve_command()) + "\r\n"

    def plan(self) -> Plan:
        return Plan(kind=self.kind, path=self.path, body=self.script(), steps=[])

    def install(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.script(), encoding="utf-8", newline="")

    def uninstall(self) -> None:
        self.path.unlink(missing_ok=True)

    def status(self) -> str:
        if not self.path.is_file():
            return MISSING
        # Nothing here manages the process, so the only honest answer to
        # "is it running" is whether a warden is on the port.
        settings = Settings()
        host = settings.host if settings.host not in {"0.0.0.0", "::"} else "127.0.0.1"
        return RUNNING if is_bound(host, settings.port) else INSTALLED


def autostart_for(system: str | None = None) -> Autostart:
    """The right one for this machine, or a plain refusal on anything else."""
    system = system or platform.system()
    if system == "Linux":
        return Systemd()
    if system == "Darwin":
        return Launchd()
    if system == "Windows":
        return StartupCommand()
    raise NotPermittedError(
        f"warden does not know how to start itself at login on {system} - "
        "run `warden serve` from whatever this system uses"
    )

"""The command line's own plumbing: the app itself, and what every command needs."""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from warden import __version__, theme
from warden.client import WardenClient
from warden.core import store

# Reached through this module rather than imported by name, so that a test
# standing in for it stands in for it everywhere. The redundant alias is how
# a re-export is spelled.
from warden.core.events import send_one as send_one
from warden.errors import WardenError
from warden.models import Registration
from warden.ports.listeners import GONE

app = typer.Typer(
    add_completion=True,
    help="Nothing binds a port without asking. A registry that hands out local ports.",
)
console = Console()
errors = Console(stderr=True)

UrlOption = Annotated[
    str | None,
    typer.Option("--url", "-u", help="Base URL of the registry.", envvar="WARDEN_URL"),
]
TokenOption = Annotated[
    str | None,
    typer.Option("--token", help="API token, if the registry requires one.",
                 envvar="WARDEN_TOKEN"),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print raw JSON.")]

# Read by more than one group of commands, so it belongs to neither.
ACTION_COLOURS = {
    store.REGISTERED: theme.MOSS,
    store.RENEWED: theme.BONE_DIM,
    store.MOVED: theme.AMETHYST,
    store.RELEASED: theme.SHRIEKER,
    store.EXPIRED: theme.EMBER,
}

NodeOption = Annotated[
    str | None,
    typer.Option("--node", help="Do this on that warden in the fleet, through this one."),
]


def _client(url: str | None, token: str | None) -> WardenClient:
    return WardenClient(url, token=token)


def _fail(exc: WardenError) -> typer.Exit:
    errors.print(exc.message, style=theme.EMBER)
    return typer.Exit(1)


def _dump(payload: object) -> None:
    console.print_json(json.dumps(payload, default=str))


def _greet() -> None:
    console.print(theme.banner_text(getattr(console.file, "encoding", None)))
    console.print(theme.TAGLINE, style=theme.BONE_DIM)
    console.print()


def _address(service: Registration) -> Text:
    text = Text(f"{service.host}:", style=theme.BONE_DIM)
    text.append(str(service.port), style=theme.GLOW)
    return text


def _holder(service: Registration) -> Text:
    gone = service.holder == GONE
    return Text(service.holder or "-", style=theme.EMBER if gone else theme.MOSS)


def _table(services: list[Registration], *, holders: bool = False) -> Table:
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    columns = ["SERVICE", "KIND", "PROJECT", "ADDRESS", "PID"]
    if holders:
        columns.append("HOLDER")
    for column in columns:
        table.add_column(column)
    for service in services:
        row = [
            service.name,
            Text(service.kind, style=theme.kind_colour(service.kind)),
            Text(service.project or "-", style=theme.BONE_DIM),
            _address(service),
            Text(str(service.pid) if service.pid else "-", style=theme.BONE_DIM),
        ]
        if holders:
            row.append(_holder(service))
        table.add_row(*row)
    return table


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", is_eager=True, help="Print the version and exit.")
    ] = False,
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit
    if ctx.invoked_subcommand is None:
        _greet()
        console.print(ctx.get_help())
        raise typer.Exit

def _has_a_screen() -> bool:
    """Whether a full-screen program can be drawn where this is running.

    A terminal on its own is not enough. A machine with TERM unset or set to
    dumb - a bare cron job, a serial console, some build runners - cannot draw
    one, and finding that out from a traceback helps nobody.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if sys.platform == "win32":
        return True
    return os.environ.get("TERM", "") not in {"", "dumb"}

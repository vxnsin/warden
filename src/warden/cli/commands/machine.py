"""Reading the machine itself, registry or no registry."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from warden import theme
from warden.cli import shared
from warden.cli.shared import (
    JsonOption,
    TokenOption,
    UrlOption,
    _dump,
    _fail,
    app,
    console,
    errors,
)
from warden.errors import WardenError
from warden.models import (
    FleetListeners,
)
from warden.ports.listeners import holder_of, listeners, stop

ORDER = 60

# Which sockets warden itself handed out, so strangers stand out in the list.
Owners = dict[tuple[str, str, int], str]


def _registered_names(url: str | None, token: str | None) -> Owners:
    """Which sockets warden itself handed out, so strangers stand out in the list.

    A warden need not be running for `warden ports` to work at all, so a registry
    that cannot be reached simply adds nothing. Keyed by node as well: the same
    port on two machines is two processes, and naming one after the other would
    be wrong rather than merely unhelpful.
    """
    try:
        with shared._client(url, token) as client:
            return {("", s.host, s.port): s.name for s in client.services()}
    except WardenError:
        return {}


def _ports_table(rows: list, known: Owners, *, fleet: bool) -> Table:
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    columns = ("PORT", "PROTO", "PROCESS", "PID", "USER", "ADDRESS", "WARDEN")
    for column in (("NODE", *columns) if fleet else columns):
        table.add_column(column)
    for row in rows:
        node = row.node if fleet else ""
        cells = (
            Text(str(row.port), style=theme.GLOW),
            Text(row.protocol, style=theme.BONE_DIM),
            row.process or Text("unknown", style=theme.BONE_DIM),
            Text(str(row.pid) if row.pid else "-", style=theme.BONE_DIM),
            Text(theme.account(row.user), style=theme.BONE_DIM),
            Text(row.host, style=theme.BONE_DIM),
            Text(known.get((node, row.host, row.port), "-"), style=theme.MOSS),
        )
        table.add_row(*((node, *cells) if fleet else cells))
    return table


def _fleet_ports(
    url: str | None, token: str | None, *, udp: bool
) -> tuple[FleetListeners, Owners]:
    """Every socket the fleet has bound, and which of them warden handed out."""
    with shared._client(url, token) as client:
        found = client.fleet_listeners(udp=udp)
        known = {
            (s.node, s.host, s.port): s.name for s in client.fleet_services().services
        }
    return found, known


@app.command()
def ports(
    port: Annotated[int | None, typer.Option(help="Only this port.")] = None,
    udp: Annotated[bool, typer.Option("--udp/--no-udp", help="Include UDP sockets.")] = True,
    every: Annotated[
        bool, typer.Option("--all", help="Ask every warden in the fleet, not just this machine.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show what is listening on this machine.

    Without `--all` this reads the machine directly and needs no warden running
    anywhere. With it, the sockets come from every warden in the fleet instead.
    """
    fleet = None
    try:
        if every:
            fleet, known = _fleet_ports(url, token, udp=udp)
            rows: list = list(fleet.listeners)
        else:
            rows = list(listeners(udp=udp))
            known = _registered_names(url, token)
    except WardenError as exc:
        raise _fail(exc) from exc
    if port is not None:
        rows = [row for row in rows if row.port == port]
        # The dump is built from the rows the table would show, or --port would
        # quietly mean nothing the moment --json was asked for as well.
        if fleet:
            fleet = fleet.model_copy(update={"listeners": rows})

    if as_json:
        _dump(
            fleet.model_dump(mode="json")
            if fleet
            else [row.model_dump(mode="json") for row in rows]
        )
        return
    if not rows:
        console.print("nothing is listening" if port is None else f"nothing on port {port}",
                      style=theme.BONE_DIM)
    else:
        console.print(_ports_table(rows, known, fleet=bool(fleet)))

    unnamed = sum(1 for row in rows if row.process is None)
    if unnamed:
        console.print(
            f"\n{unnamed} of {len(rows)} belong to another user - "
            "run warden as administrator to see them",
            style=theme.SHRIEKER,
        )
    for missing in fleet.unreachable if fleet else []:
        errors.print(f"{missing.node} ({missing.url}) {missing.reason}", style=theme.SHRIEKER)


@app.command()
def kill(
    target: Annotated[int, typer.Argument(help="A port to free, or a process id with --pid.")],
    pid: Annotated[bool, typer.Option("--pid", help="Read the number as a process id.")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Kill it outright if it will not stop politely.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
) -> None:
    """Stop whatever is holding a port."""
    try:
        if pid:
            target_pid, description = target, f"process {target}"
        else:
            holder = holder_of(target)
            if holder is None:
                errors.print(f"nothing is listening on port {target}", style=theme.EMBER)
                raise typer.Exit(1)
            if holder.pid is None:
                errors.print(
                    f"port {target} is held by a process this user may not see - "
                    "run warden as administrator",
                    style=theme.EMBER,
                )
                raise typer.Exit(1)
            target_pid = holder.pid
            description = f"{holder.process} ({holder.pid}) on port {target}"

        if not yes and not typer.confirm(f"Stop {description}?"):
            console.print("left alone", style=theme.BONE_DIM)
            return

        name = stop(target_pid, force=force)
    except WardenError as exc:
        raise _fail(exc) from exc
    console.print(f"stopped {name} ({target_pid})")

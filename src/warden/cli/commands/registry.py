"""Asking for a port, keeping it, and giving it back."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from warden import theme
from warden.cli import shared
from warden.cli.shared import (
    ACTION_COLOURS,
    JsonOption,
    NodeOption,
    TokenOption,
    UrlOption,
    _address,
    _dump,
    _fail,
    _table,
    app,
    console,
    errors,
)
from warden.errors import WardenError
from warden.models import (
    Event,
    FleetPool,
    FleetRegistration,
    PoolStatus,
    Registration,
)
from warden.ports import runner
from warden.ports.listeners import GONE

ORDER = 20


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Option(help="Register under this name.")] = None,
    kind: Annotated[str, typer.Option("--kind", "-k", help="What the service is.")] = "service",
    project: Annotated[str | None, typer.Option(help="Group services of one codebase.")] = None,
    host: Annotated[str, typer.Option(help="Interface the process will bind to.")] = "127.0.0.1",
    preferred_port: Annotated[
        int | None, typer.Option(help="Wish for this port, take another if it is not free.")
    ] = None,
    require_port: Annotated[
        int | None, typer.Option(help="Insist on this port, and fail if it is not free.")
    ] = None,
    ttl: Annotated[
        int | None, typer.Option(help="Hold a lease this long, renewed while the process runs.")
    ] = None,
    anyway: Annotated[
        bool, typer.Option("--anyway", help="Start on a free port when no warden answers.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
) -> None:
    """Start a command on a port warden picked for it.

    Everything after `--` is the command. The process gets PORT in its
    environment, keeps the port while it runs, and gives it back when it exits.
    """
    command = list(ctx.args)
    if not command:
        raise _fail(WardenError("nothing to run - put the command after `--`"))

    name = name or runner.default_name()
    client = shared._client(url, token)
    try:
        service = client.register(
            name,
            kind=kind,
            project=project,
            host=host,
            preferred_port=preferred_port,
            require_port=require_port,
            ttl=ttl,
        )
        registered = True
    except WardenError as exc:
        if not anyway:
            errors.print(exc.message, style=theme.EMBER)
            errors.print("or pass --anyway to start on a free port", style=theme.BONE_DIM)
            client.close()
            raise typer.Exit(1) from exc
        service = _unregistered(name, kind, host)
        registered = False

    _announce(service, registered)
    beat = None
    if registered and ttl:
        beat = runner.Heartbeat(client, name, ttl)
        beat.start()
    def note_pid(pid: int) -> None:
        with suppress(WardenError):
            client.heartbeat(name, pid=pid, ttl=ttl)

    try:
        code = runner.supervise(command, service, note_pid if registered else None)
    finally:
        # In a finally so a crash, a clean exit and Ctrl-C all give the port back.
        if beat:
            beat.stop()
        if registered:
            with suppress(WardenError):
                client.release(name)
        client.close()
    raise typer.Exit(code)


def _unregistered(name: str, kind: str, host: str) -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind=kind,
        project=None,
        host=host,
        port=runner.free_port(host),
        pid=None,
        meta={},
        ttl=None,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )


def _announce(service: Registration, registered: bool) -> None:
    line = Text(service.name, style=theme.BONE)
    line.append("  ->  ", style=theme.VEIN_BRIGHT)
    line.append(str(service.port), style=theme.GLOW)
    if not registered:
        line.append("   unregistered, no warden running", style=theme.SHRIEKER)
    errors.print(line)


@app.command("env")
def environment(
    name: Annotated[str | None, typer.Argument(help="Register under this name.")] = None,
    kind: Annotated[str, typer.Option("--kind", "-k", help="What the service is.")] = "service",
    project: Annotated[str | None, typer.Option(help="Group services of one codebase.")] = None,
    host: Annotated[str, typer.Option(help="Interface the service will bind to.")] = "127.0.0.1",
    preferred_port: Annotated[
        int | None, typer.Option(help="Wish for this port, take another if it is not free.")
    ] = None,
    require_port: Annotated[
        int | None, typer.Option(help="Insist on this port, and fail if it is not free.")
    ] = None,
    export: Annotated[
        bool, typer.Option("--export", help="Prefix each line for `eval $(warden env ...)`.")
    ] = False,
    write: Annotated[
        Path | None, typer.Option(help="Update these keys in a dotenv file, leaving the rest.")
    ] = None,
    url: UrlOption = None,
    token: TokenOption = None,
) -> None:
    """Claim a port and print it as environment, for what cannot be wrapped."""
    with shared._client(url, token) as client:
        try:
            service = client.register(
                name or runner.default_name(),
                kind=kind,
                project=project,
                host=host,
                preferred_port=preferred_port,
                require_port=require_port,
            )
        except WardenError as exc:
            raise _fail(exc) from exc

    values = runner.as_env(service)
    if write:
        before = write.read_text(encoding="utf-8") if write.is_file() else ""
        write.write_text(runner.merge_dotenv(before, values), encoding="utf-8")
        errors.print(f"{write}  ->  PORT={service.port}", style=theme.BONE_DIM)
        return
    prefix = "export " if export else ""
    for key, value in values.items():
        console.print(f"{prefix}{key}={value}", highlight=False)


def _fleet_table(services: list[FleetRegistration]) -> Table:
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("NODE", "SERVICE", "KIND", "PROJECT", "ADDRESS", "PID"):
        table.add_column(column)
    for service in services:
        table.add_row(
            service.node,
            service.name,
            Text(service.kind, style=theme.kind_colour(service.kind)),
            Text(service.project or "-", style=theme.BONE_DIM),
            _address(service),
            Text(str(service.pid) if service.pid else "-", style=theme.BONE_DIM),
        )
    return table


@app.command("ls")
def list_services(
    project: Annotated[str | None, typer.Option(help="Only this project.")] = None,
    kind: Annotated[str | None, typer.Option(help="Only this kind of service.")] = None,
    holders: Annotated[
        bool, typer.Option("--holders", help="Also say whether each holder is still there.")
    ] = False,
    stale: Annotated[
        bool, typer.Option("--stale", help="Only services whose holder is gone.")
    ] = False,
    every: Annotated[
        bool, typer.Option("--all", help="Ask every warden in the fleet, not just this one.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """List every registered service."""
    holders = holders or stale
    if holders and every:
        raise _fail(
            WardenError("a holder can only be checked on the machine it runs on, so --holders "
                        "asks one warden at a time")
        )
    with shared._client(url, token) as client:
        try:
            fleet = (
                client.fleet_services(project=project, kind=kind)
                if every
                else None
            )
            services = (
                fleet.services
                if fleet
                else client.services(project=project, kind=kind, holders=holders)
            )
        except WardenError as exc:
            raise _fail(exc) from exc

    if stale:
        services = [service for service in services if service.holder == GONE]

    if as_json:
        _dump(
            fleet.model_dump(mode="json")
            if fleet
            else [service.model_dump(mode="json") for service in services]
        )
        return
    if services:
        console.print(_fleet_table(services) if fleet else _table(services, holders=holders))
    elif stale:
        console.print("every registered service is still there", style=theme.BONE_DIM)
    else:
        console.print("nothing registered", style=theme.BONE_DIM)

    # Said out loud, because a shorter list because a machine was down reads
    # exactly like a shorter list because nothing is registered there.
    for missing in fleet.unreachable if fleet else []:
        errors.print(f"{missing.node} ({missing.url}) {missing.reason}", style=theme.SHRIEKER)

    # Unique is a promise each node makes on its own, so a name held twice is
    # only ever visible from here - and it is nearly always a mistake.
    for clash in fleet.duplicates if fleet else []:
        errors.print(
            f"{clash.name} is registered on {theme.listed(clash.nodes)}", style=theme.SHRIEKER
        )




def _history_table(events: list[Event]) -> Table:
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("WHEN", "WHAT", "SERVICE", "KIND", "ADDRESS", "PID"):
        table.add_column(column)
    for event in events:
        table.add_row(
            Text(theme.age(event.at), style=theme.BONE_DIM),
            Text(event.action, style=ACTION_COLOURS.get(event.action, theme.BONE)),
            event.name,
            Text(event.kind, style=theme.kind_colour(event.kind)),
            event.address,
            Text(str(event.pid) if event.pid else "-", style=theme.BONE_DIM),
        )
    return table


@app.command()
def history(
    what: Annotated[
        str | None,
        typer.Argument(help="A port, or a service name. Everything recent without one."),
    ] = None,
    limit: Annotated[int, typer.Option(help="How many to show.")] = 20,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """What happened to a port, kept after it stopped being true.

    The registry only knows what is true now, and "what had 8000 yesterday" is
    a question people ask far more often than that allows for.
    """
    port = int(what) if what and what.isdigit() else None
    name = what if what and not what.isdigit() else None

    with shared._client(url, token) as client:
        try:
            events = client.history(port=port, name=name, limit=limit)
        except WardenError as exc:
            raise _fail(exc) from exc

    if as_json:
        _dump([event.model_dump(mode="json") for event in events])
        return
    if not events:
        console.print(
            f"nothing recorded for {what}" if what else "nothing recorded yet",
            style=theme.BONE_DIM,
        )
        return
    console.print(_history_table(events))


@app.command()
def reap(
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Release them all without asking.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
) -> None:
    """Release the ports of services that are no longer there.

    Never automatic: a service that is restarting would lose its port to a
    timer, so which registrations go is a person's decision.
    """
    with shared._client(url, token) as client:
        try:
            gone = [
                service
                for service in client.services(holders=True)
                if service.holder == GONE
            ]
        except WardenError as exc:
            raise _fail(exc) from exc

        if not gone:
            console.print("every registered service is still there", style=theme.BONE_DIM)
            return

        released = 0
        for service in gone:
            reason = service.holder_reason or "it is no longer there"
            if not yes and not typer.confirm(f"release {service.name}? {reason}"):
                continue
            try:
                client.release(service.name)
            except WardenError as exc:
                errors.print(f"{service.name}: {exc}", style=theme.SHRIEKER)
                continue
            released += 1
            if yes:
                console.print(f"released {service.name} - {reason}")

    console.print(
        f"released {released} of {len(gone)}"
        if released != len(gone)
        else f"released {released}",
        style=theme.BONE_DIM,
    )


@app.command()
def get(
    name: str,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Print the address of one service.

    Give it as `node/service` to ask a particular warden in the fleet.
    """
    node, _, service_name = name.rpartition("/")
    with shared._client(url, token) as client:
        try:
            service = (
                client.fleet_lookup(node, service_name) if node else client.lookup(name)
            )
        except WardenError as exc:
            raise _fail(exc) from exc
    if as_json:
        _dump(service.model_dump(mode="json"))
    else:
        console.print(service.address)


@app.command()
def register(
    name: str,
    kind: Annotated[str, typer.Option("--kind", "-k", help="What the service is.")],
    project: Annotated[str | None, typer.Option(help="Group services of one project.")] = None,
    host: Annotated[str, typer.Option(help="Interface the service will bind to.")] = "127.0.0.1",
    preferred_port: Annotated[
        int | None,
        typer.Option(help="Wish for this port, but take another one if it is not free."),
    ] = None,
    require_port: Annotated[
        int | None,
        typer.Option(help="Insist on this port, and fail if it is not free."),
    ] = None,
    ttl: Annotated[
        int | None, typer.Option(help="Release the port again after this many seconds.")
    ] = None,
    pid: Annotated[int | None, typer.Option(help="Process id of the service.")] = None,
    count: Annotated[
        int | None,
        typer.Option(help="Claim this many ports at once, named <name>-1 upwards."),
    ] = None,
    contiguous: Annotated[
        bool,
        typer.Option("--contiguous", help="With --count, insist they run back to back."),
    ] = False,
    node: NodeOption = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Claim a port and print it.

    With `--count` it claims several at once, named `<name>-1` upwards, and
    either holds all of them or none. With `--node` a single request goes
    through this warden to that one, which is still the machine that decides.
    """
    if count is not None:
        if node:
            raise _fail(WardenError("--count registers here; ask that warden directly instead"))
        if preferred_port or require_port:
            raise _fail(
                WardenError("a wish for one port and a request for several do not go together")
            )
        with shared._client(url, token) as client:
            try:
                group = client.register_group(
                    name,
                    kind=kind,
                    count=count,
                    contiguous=contiguous,
                    project=project,
                    host=host,
                    ttl=ttl,
                    pid=pid,
                )
            except WardenError as exc:
                raise _fail(exc) from exc
        if as_json:
            _dump([service.model_dump(mode="json") for service in group])
        else:
            for service in group:
                console.print(service.port)
        return

    with shared._client(url, token) as client:
        try:
            service = client.register(
                name,
                kind=kind,
                project=project,
                host=host,
                preferred_port=preferred_port,
                require_port=require_port,
                ttl=ttl,
                pid=pid,
                node=node,
            )
        except WardenError as exc:
            raise _fail(exc) from exc
    if as_json:
        _dump(service.model_dump(mode="json"))
    else:
        console.print(service.port)




Owners = dict[tuple[str, str, int], str]


@app.command()
def release(
    name: str, node: NodeOption = None, url: UrlOption = None, token: TokenOption = None
) -> None:
    """Give a port back to the pool."""
    with shared._client(url, token) as client:
        try:
            client.release(name, node=node)
        except WardenError as exc:
            raise _fail(exc) from exc
    console.print(f"released {name}" if node is None else f"released {node}/{name}")


@app.command()
def heartbeat(
    name: str,
    ttl: Annotated[
        int | None, typer.Option(help="Keep it for this many seconds from now.")
    ] = None,
    pid: Annotated[int | None, typer.Option(help="Process id of the service.")] = None,
    node: NodeOption = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Push a lease out again before it runs out.

    Without a ttl it renews the lease the service registered with, so a
    heartbeat can never turn a lease into a permanent registration by accident.
    """
    with shared._client(url, token) as client:
        try:
            service = client.heartbeat(name, ttl=ttl, pid=pid, node=node)
        except WardenError as exc:
            raise _fail(exc) from exc
    if as_json:
        _dump(service.model_dump(mode="json"))
    elif service.expires_at:
        # How long is left, not a wall clock: the registry keeps UTC and the
        # person reading this is somewhere else.
        console.print(
            f"{service.name} holds {service.port} for another "
            f"{theme.until(service.expires_at)}"
        )
    else:
        console.print(f"{service.name} holds {service.port} with no lease to renew")


def _free(status: PoolStatus) -> Text:
    """What is left, coloured by how little that is.

    The point of the fleet view is spotting the machine about to run out, and a
    column of identical numbers hides exactly that.
    """
    capacity = status.allocated + status.available
    if status.available <= 0:
        style = theme.EMBER
    elif capacity and status.available * 10 <= capacity:
        style = theme.SHRIEKER
    else:
        style = theme.MOSS
    return Text(str(status.available), style=style)


def _show_fleet_pool(fleet: FleetPool, *, as_json: bool) -> None:
    if as_json:
        _dump(fleet.model_dump(mode="json"))
    else:
        table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
        for column in ("NODE", "POOL", "HELD", "FREE", "RESERVED"):
            table.add_column(column)
        for status in fleet.pools:
            table.add_row(
                status.node,
                Text(f"{status.start}-{status.end}", style=theme.GLOW),
                str(status.allocated),
                _free(status),
                Text(str(len(status.reserved)), style=theme.BONE_DIM),
            )
        console.print(table)
        console.print()
        console.print(
            f"{theme.plural(len(fleet.pools), 'warden')}  "
            f"{fleet.allocated} allocated  {fleet.available} free  "
            f"of {fleet.capacity}",
            style=theme.BONE_DIM,
        )

    for missing in fleet.unreachable:
        errors.print(f"{missing.node} ({missing.url}) {missing.reason}", style=theme.SHRIEKER)


@app.command()
def pool(
    every: Annotated[
        bool, typer.Option("--all", help="Ask every warden in the fleet, not just this one.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show how much of the pool is in use."""
    with shared._client(url, token) as client:
        try:
            if every:
                _show_fleet_pool(client.fleet_pool(), as_json=as_json)
                return
            status = client.pool()
        except WardenError as exc:
            raise _fail(exc) from exc
    if as_json:
        _dump(status.model_dump(mode="json"))
    else:
        line = (
            f"{status.start}-{status.end}  "
            f"{status.allocated} allocated  {status.available} free  "
            f"{len(status.reserved)} reserved"
        )
        # Only when it is the smaller number: that is the case where saying how
        # many are free on their own would be misleading.
        if status.largest_run < status.available:
            line += f"  {status.largest_run} in a row"
        console.print(line)

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from warden import __version__, theme
from warden.client import WardenClient
from warden.config import Settings
from warden.errors import WardenError
from warden.listeners import holder_of, listeners, stop
from warden.models import Registration

app = typer.Typer(
    add_completion=False,
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


def _table(services: list[Registration]) -> Table:
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("SERVICE", "KIND", "PROJECT", "ADDRESS", "PID"):
        table.add_column(column)
    for service in services:
        table.add_row(
            service.name,
            Text(service.kind, style=theme.kind_colour(service.kind)),
            Text(service.project or "-", style=theme.BONE_DIM),
            _address(service),
            Text(str(service.pid) if service.pid else "-", style=theme.BONE_DIM),
        )
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


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Interface the registry listens on.")] = None,
    port: Annotated[int | None, typer.Option(help="Port the registry listens on.")] = None,
    pool: Annotated[
        str | None, typer.Option(help="Range of ports to hand out, e.g. 8000-8999.")
    ] = None,
    reserved: Annotated[
        str | None, typer.Option(help="Ports never handed out, e.g. 8080,9000-9010.")
    ] = None,
    database: Annotated[Path | None, typer.Option(help="Path to the registry database.")] = None,
    no_probe: Annotated[
        bool, typer.Option("--no-probe", help="Do not test ports for existing listeners.")
    ] = False,
) -> None:
    """Run the registry."""
    import uvicorn

    from warden.api import create_app

    overrides: dict[str, object] = {}
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    if database is not None:
        overrides["database"] = database
    if reserved is not None:
        overrides["reserved"] = reserved
    if no_probe:
        overrides["probe"] = False
    if pool is not None:
        start, _, end = pool.partition("-")
        overrides["pool_start"] = int(start)
        overrides["pool_end"] = int(end or start)

    settings = Settings(**overrides)
    _greet()
    console.print(f"v{__version__}  listening on {settings.url}", style=theme.BONE_DIM)
    console.print(
        f"pool {settings.pool_start}-{settings.pool_end}"
        f"  {len(settings.reserved)} reserved",
        style=theme.BONE_DIM,
    )
    console.print()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


@app.command()
def tui(
    url: UrlOption = None,
    token: TokenOption = None,
    interval: Annotated[float, typer.Option(help="Refresh interval in seconds.")] = 2.0,
) -> None:
    """Open the terminal dashboard."""
    from warden.tui import run

    run(url, token=token, interval=interval)


@app.command("ls")
def list_services(
    project: Annotated[str | None, typer.Option(help="Only this project.")] = None,
    kind: Annotated[str | None, typer.Option(help="Only this kind of service.")] = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """List every registered service."""
    with _client(url, token) as client:
        try:
            services = client.services(project=project, kind=kind)
        except WardenError as exc:
            raise _fail(exc) from exc
    if as_json:
        _dump([service.model_dump(mode="json") for service in services])
    elif services:
        console.print(_table(services))
    else:
        console.print("nothing registered", style=theme.BONE_DIM)


@app.command()
def get(
    name: str,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Print the address of one service."""
    with _client(url, token) as client:
        try:
            service = client.lookup(name)
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
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Claim a port and print it."""
    with _client(url, token) as client:
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
            )
        except WardenError as exc:
            raise _fail(exc) from exc
    if as_json:
        _dump(service.model_dump(mode="json"))
    else:
        console.print(service.port)




def _registered_names(url: str | None, token: str | None) -> dict[tuple[str, int], str]:
    """Which sockets warden itself handed out, so strangers stand out in the list.

    A warden need not be running for `warden ports` to work at all, so a registry
    that cannot be reached simply adds nothing.
    """
    try:
        with _client(url, token) as client:
            return {(service.host, service.port): service.name for service in client.services()}
    except WardenError:
        return {}


@app.command()
def ports(
    port: Annotated[int | None, typer.Option(help="Only this port.")] = None,
    udp: Annotated[bool, typer.Option("--udp/--no-udp", help="Include UDP sockets.")] = True,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show what is listening on this machine."""
    try:
        rows = listeners(udp=udp)
    except WardenError as exc:
        raise _fail(exc) from exc
    if port is not None:
        rows = [row for row in rows if row.port == port]

    if as_json:
        _dump([row.model_dump(mode="json") for row in rows])
        return
    if not rows:
        console.print("nothing is listening" if port is None else f"nothing on port {port}",
                      style=theme.BONE_DIM)
        return

    known = _registered_names(url, token)
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("PORT", "PROTO", "PROCESS", "PID", "USER", "ADDRESS", "WARDEN"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            Text(str(row.port), style=theme.GLOW),
            Text(row.protocol, style=theme.BONE_DIM),
            row.process or Text("unknown", style=theme.BONE_DIM),
            Text(str(row.pid) if row.pid else "-", style=theme.BONE_DIM),
            Text(theme.account(row.user), style=theme.BONE_DIM),
            Text(row.host, style=theme.BONE_DIM),
            Text(known.get((row.host, row.port), "-"), style=theme.MOSS),
        )
    console.print(table)

    unnamed = sum(1 for row in rows if row.process is None)
    if unnamed:
        console.print(
            f"\n{unnamed} of {len(rows)} belong to another user - "
            "run warden as administrator to see them",
            style=theme.SHRIEKER,
        )


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


@app.command()
def release(name: str, url: UrlOption = None, token: TokenOption = None) -> None:
    """Give a port back to the pool."""
    with _client(url, token) as client:
        try:
            client.release(name)
        except WardenError as exc:
            raise _fail(exc) from exc
    console.print(f"released {name}")


NODE_COLOURS = {"online": theme.MOSS, "stale": theme.SHRIEKER}


@app.command()
def nodes(
    forget: Annotated[
        str | None, typer.Option(help="Remove a warden that is not coming back.")
    ] = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """List the wardens this one knows about."""
    with _client(url, token) as client:
        try:
            if forget:
                client.forget(forget)
                console.print(f"forgot {forget}", style=theme.BONE_DIM)
            known = client.nodes()
        except WardenError as exc:
            raise _fail(exc) from exc

    if as_json:
        _dump([node.model_dump(mode="json") for node in known])
        return
    if not known:
        console.print("no other warden has reported in", style=theme.BONE_DIM)
        return

    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("NODE", "URL", "POOL", "VERSION", "STATUS", "LAST SEEN"):
        table.add_column(column)
    for node in known:
        table.add_row(
            node.name,
            Text(node.url, style=theme.BONE_DIM),
            Text(node.pool, style=theme.GLOW),
            Text(node.version, style=theme.BONE_DIM),
            Text(node.status, style=NODE_COLOURS[node.status]),
            Text(theme.age(node.last_seen), style=theme.BONE_DIM),
        )
    console.print(table)


@app.command()
def pool(url: UrlOption = None, token: TokenOption = None, as_json: JsonOption = False) -> None:
    """Show how much of the pool is in use."""
    with _client(url, token) as client:
        try:
            status = client.pool()
        except WardenError as exc:
            raise _fail(exc) from exc
    if as_json:
        _dump(status.model_dump(mode="json"))
    else:
        console.print(
            f"{status.start}-{status.end}  "
            f"{status.allocated} allocated  {status.available} free  "
            f"{len(status.reserved)} reserved"
        )


def main() -> None:
    app()

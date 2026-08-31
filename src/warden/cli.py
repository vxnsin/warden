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


@app.command()
def release(name: str, url: UrlOption = None, token: TokenOption = None) -> None:
    """Give a port back to the pool."""
    with _client(url, token) as client:
        try:
            client.release(name)
        except WardenError as exc:
            raise _fail(exc) from exc
    console.print(f"released {name}")


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

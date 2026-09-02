from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, get_type_hints

import typer
from pydantic import TypeAdapter, ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from warden import __version__, config, runner, theme
from warden.client import WardenClient
from warden.config import Settings
from warden.errors import WardenError
from warden.listeners import holder_of, listeners, stop
from warden.models import (
    FleetListeners,
    FleetPool,
    FleetRegistration,
    FleetUpdate,
    PoolStatus,
    Registration,
    UpdateStatus,
)

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


SECRETS = {"token", "cluster_token"}

# The full annotations, so a set of ports keeps the parser that turns
# "8080,9000-9010" into one.
FIELD_TYPES = get_type_hints(Settings, include_extras=True)

ORIGIN_COLOURS = {
    "environment": theme.SHRIEKER,
    ".env": theme.SHRIEKER,
    "config file": theme.MOSS,
    "default": theme.BONE_DIM,
}


def _shown(field: str, value: object) -> Text:
    if field in SECRETS and value:
        return Text("set", style=theme.MOSS)
    if value is None or value == "" or value == set():
        return Text("-", style=theme.BONE_DIM)
    if isinstance(value, set):
        value = ",".join(str(item) for item in sorted(value))
    return Text(str(value))


@app.command()
def setup() -> None:
    """Ask the few questions that matter and write the answers down."""
    _greet()
    current = Settings()
    console.print(f"Settings go to {config.config_file()}", style=theme.BONE_DIM)
    console.print()

    answers: dict[str, object] = dict(config.stored())
    pool = typer.prompt("Ports to hand out", default=f"{current.pool_start}-{current.pool_end}")
    start, _, end = pool.partition("-")
    answers["pool_start"], answers["pool_end"] = int(start), int(end or start)

    reserved = typer.prompt("Ports never to hand out", default="", show_default=False)
    if reserved.strip():
        answers["reserved"] = sorted(config.parse_ports(reserved))
    answers["port"] = typer.prompt("Port warden itself listens on", default=current.port, type=int)

    if typer.confirm("Reachable from other machines?", default=current.host != "127.0.0.1"):
        answers["host"] = "0.0.0.0"
        console.print(
            "  Listening beyond this machine, so it needs a token.", style=theme.SHRIEKER
        )
        answers["token"] = typer.prompt("  Token callers must send", default=current.token or "")
    else:
        answers["host"] = "127.0.0.1"

    if typer.confirm("Does this warden report to another one?", default=bool(current.upstream)):
        answers["upstream"] = typer.prompt(
            "  Address of that warden", default=current.upstream or ""
        )
        answers["node"] = typer.prompt("  Name for this machine", default=current.node)
        answers["advertise"] = typer.prompt(
            "  Address it should use to reach this one", default=current.advertise_url
        )
        answers["cluster_token"] = typer.prompt(
            "  Shared secret between wardens", default=current.cluster_token or ""
        )

    if typer.confirm("Allow stopping processes over the API?", default=current.allow_kill):
        answers["allow_kill"] = True

    written = config.write(answers)
    console.print()
    console.print(f"Written to {written}", style=theme.MOSS)
    console.print("`warden settings` shows what is in effect.", style=theme.BONE_DIM)


settings_app = typer.Typer(help="Show and change what is written down.")
app.add_typer(settings_app, name="settings", invoke_without_command=True)


@settings_app.callback(invoke_without_command=True)
def settings_list(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Show every setting, its value, and where that value came from."""
    if ctx.invoked_subcommand is not None:
        return
    current = Settings()
    if as_json:
        _dump(
            {
                field: {"value": str(value), "from": config.origin(field)}
                for field, value in current.model_dump().items()
            }
        )
        return

    console.print(f"{config.config_file()}", style=theme.BONE_DIM)
    console.print()
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("SETTING", "VALUE", "FROM"):
        table.add_column(column)
    for field, value in current.model_dump().items():
        source = config.origin(field)
        table.add_row(field, _shown(field, value), Text(source, style=ORIGIN_COLOURS[source]))
    console.print(table)


@settings_app.command("set")
def settings_set(field: str, value: str) -> None:
    """Write one setting to the config file."""
    if field not in Settings.model_fields:
        raise _fail(WardenError(f"no setting called {field!r}; `warden settings` lists them all"))
    try:
        # Typed before it is written, so the file holds 4000 rather than "4000",
        # then checked against the rest of the settings it has to live with.
        typed = TypeAdapter(FIELD_TYPES[field]).validate_python(value)
        checked = Settings(**{field: typed})
    except ValidationError as exc:
        raise _fail(WardenError(f"{field}: {exc.errors()[0]['msg']}")) from exc
    # What the file records is what warden will read back. Not for reserved,
    # where the settings add the API port and only what was asked for belongs.
    keep = typed if field == "reserved" else getattr(checked, field)
    config.write({**config.stored(), field: keep})
    console.print(f"{field} = {value}", style=theme.MOSS)
    if config.origin(field) != "config file":
        console.print(
            f"Note: {config.origin(field)} still wins over the file for this one.",
            style=theme.SHRIEKER,
        )


@settings_app.command("unset")
def settings_unset(field: str) -> None:
    """Take one setting back out of the config file."""
    stored = config.stored()
    if field not in stored:
        raise _fail(WardenError(f"{field} is not in the config file"))
    del stored[field]
    config.write(stored)
    console.print(f"{field} removed, back to its default", style=theme.BONE_DIM)


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
    client = _client(url, token)
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
    with _client(url, token) as client:
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
    every: Annotated[
        bool, typer.Option("--all", help="Show the whole fleet, not just this warden.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    interval: Annotated[float, typer.Option(help="Refresh interval in seconds.")] = 2.0,
) -> None:
    """Open the terminal dashboard.

    With `--all` both tables gain a NODE column and `n` steps the view through
    one warden at a time. A large fleet is worth a longer `--interval`, since
    every refresh asks every node.
    """
    from warden.tui import run

    run(url, token=token, interval=interval, fleet=every)


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
    every: Annotated[
        bool, typer.Option("--all", help="Ask every warden in the fleet, not just this one.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """List every registered service."""
    with _client(url, token) as client:
        try:
            fleet = (
                client.fleet_services(project=project, kind=kind)
                if every
                else None
            )
            services = fleet.services if fleet else client.services(project=project, kind=kind)
        except WardenError as exc:
            raise _fail(exc) from exc

    if as_json:
        _dump(
            fleet.model_dump(mode="json")
            if fleet
            else [service.model_dump(mode="json") for service in services]
        )
        return
    if services:
        console.print(_fleet_table(services) if fleet else _table(services))
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
    with _client(url, token) as client:
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
    node: NodeOption = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Claim a port and print it.

    With `--node` the request goes through this warden to that one, which is
    still the machine that decides.
    """
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
                node=node,
            )
        except WardenError as exc:
            raise _fail(exc) from exc
    if as_json:
        _dump(service.model_dump(mode="json"))
    else:
        console.print(service.port)




Owners = dict[tuple[str, str, int], str]


def _registered_names(url: str | None, token: str | None) -> Owners:
    """Which sockets warden itself handed out, so strangers stand out in the list.

    A warden need not be running for `warden ports` to work at all, so a registry
    that cannot be reached simply adds nothing. Keyed by node as well: the same
    port on two machines is two processes, and naming one after the other would
    be wrong rather than merely unhelpful.
    """
    try:
        with _client(url, token) as client:
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
    with _client(url, token) as client:
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


@app.command()
def release(
    name: str, node: NodeOption = None, url: UrlOption = None, token: TokenOption = None
) -> None:
    """Give a port back to the pool."""
    with _client(url, token) as client:
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
    with _client(url, token) as client:
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


@app.command()
def update(
    apply: Annotated[
        bool, typer.Option("--apply", help="Ask this warden to update itself.")
    ] = False,
    fleet: Annotated[
        bool, typer.Option("--fleet", help="Ask every warden in the fleet to update itself.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Say whether a newer warden exists, and optionally go and get it."""
    with _client(url, token) as client:
        try:
            if not (apply or fleet):
                _show_update(client.update_status(), as_json=as_json)
                return

            what = "every warden in the fleet" if fleet else f"the warden at {client.url}"
            if not yes and not typer.confirm(f"Update {what}?"):
                console.print("left alone", style=theme.BONE_DIM)
                return

            if fleet:
                _show_fleet_update(client.update_fleet(), as_json=as_json)
            else:
                detail = client.update_self()
                _dump({"detail": detail}) if as_json else console.print(detail)
        except WardenError as exc:
            raise _fail(exc) from exc


def _show_update(status: UpdateStatus, *, as_json: bool) -> None:
    if as_json:
        _dump(status.model_dump(mode="json"))
        return
    if status.available:
        console.print(f"warden {status.latest} is out, this is {status.current}", style=theme.GLOW)
        if status.url:
            console.print(status.url, style=theme.BONE_DIM)
    elif status.latest:
        console.print(f"{status.current} is the newest there is", style=theme.BONE_DIM)
    else:
        console.print(f"{status.current}; {status.reason}", style=theme.BONE_DIM)


def _show_fleet_update(result: FleetUpdate, *, as_json: bool) -> None:
    if as_json:
        _dump(result.model_dump(mode="json"))
        return
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("NODE", "RESULT", "DETAIL"):
        table.add_column(column)
    for row in result.results:
        table.add_row(
            row.node,
            Text("updated" if row.ok else "refused", style=theme.MOSS if row.ok else theme.EMBER),
            Text(row.detail, style=theme.BONE_DIM),
        )
    console.print(table)


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
    with _client(url, token) as client:
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
        console.print(
            f"{status.start}-{status.end}  "
            f"{status.allocated} allocated  {status.available} free  "
            f"{len(status.reserved)} reserved"
        )


def main() -> None:
    app()

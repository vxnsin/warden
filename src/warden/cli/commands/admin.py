"""Setting warden up, running it, and asking it what is wrong."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, get_type_hints
from urllib.parse import urlparse

import typer
from pydantic import TypeAdapter, ValidationError
from rich.table import Table
from rich.text import Text

from warden import __version__, theme
from warden.cli import shared
from warden.cli.shared import (
    JsonOption,
    TokenOption,
    UrlOption,
    _dump,
    _fail,
    _greet,
    app,
    console,
    errors,
)
from warden.core import autostart, config, health, store, webhooks
from warden.core.config import Settings
from warden.errors import WardenError

ORDER = 10


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


def _ask_address(question: str, default: str) -> str:
    """Ask again rather than write down an address nothing can post to."""
    while True:
        answer = typer.prompt(question, default=default).strip()
        if urlparse(answer).scheme in {"http", "https"}:
            return answer
        console.print("  That is not somewhere anything can be posted.", style=theme.SHRIEKER)


def _ask_one_of(question: str, options: tuple[str, ...], default: str) -> str:
    while True:
        answer = typer.prompt(f"{question} ({'/'.join(options)})", default=default)
        answer = answer.strip().lower()
        if answer in options:
            return answer
        console.print(f"  There is no {answer!r}.", style=theme.SHRIEKER)


def _ask_some_of(question: str, options: tuple[str, ...], default: list[str]) -> list[str]:
    while True:
        given = typer.prompt(
            f"{question} ({', '.join(options)})", default=",".join(default)
        )
        chosen = sorted({word.strip().lower() for word in given.split(",") if word.strip()})
        unknown = [word for word in chosen if word not in options]
        if unknown:
            console.print(f"  There is no {theme.listed(unknown)}.", style=theme.SHRIEKER)
        elif not chosen:
            console.print("  Name at least one, or answer no above.", style=theme.SHRIEKER)
        else:
            return chosen


def _ask_about_webhooks(answers: dict[str, object], current: Settings) -> None:
    """Where events go, and whether that address actually answers."""
    if not typer.confirm("Post events to a chat or a service?", default=bool(current.webhook)):
        return

    console.print(
        "  Anyone holding this address can post as you, so it belongs here and nowhere else.",
        style=theme.BONE_DIM,
    )
    answers["webhook"] = _ask_address("  Address to post to", current.webhook or "")
    shape = _ask_one_of("  Shape it should take", webhooks.FORMATS, current.webhook_format)
    answers["webhook_format"] = shape
    answers["webhook_events"] = _ask_some_of(
        "  Events worth posting", store.ACTIONS, sorted(current.webhook_events)
    )
    if shape == webhooks.JSON:
        console.print(
            "  A secret signs the body, so the far end can tell the post came from here.",
            style=theme.BONE_DIM,
        )
        answers["webhook_secret"] = typer.prompt(
            "  Secret to sign it with", default=current.webhook_secret or ""
        )

    if typer.confirm("  Post a test event now?", default=True):
        trying = current.model_copy(
            update={
                "webhook": answers["webhook"],
                "webhook_format": shape,
                "webhook_secret": answers.get("webhook_secret") or None,
                "webhook_events": set(answers["webhook_events"]),
            }
        )
        problem = shared.send_one(trying)
        if problem:
            console.print(f"  It did not arrive: {problem}", style=theme.EMBER)
            console.print(
                "  Writing it down anyway. `warden webhook --test` tries again.",
                style=theme.BONE_DIM,
            )
        else:
            console.print("  It arrived.", style=theme.MOSS)


def _setup_questions() -> dict[str, object]:
    """The same questions, one at a time, for anything without a terminal."""
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

    _ask_about_webhooks(answers, current)

    answers["allow_kill"] = typer.confirm(
        "Allow stopping processes over the API?", default=current.allow_kill
    )
    return answers


@app.command()
def setup(
    plain: Annotated[
        bool,
        typer.Option("--plain", help="Ask one question at a time instead of a screenful."),
    ] = False,
) -> None:
    """Ask the few questions that matter and write the answers down.

    A terminal gets all of them on one screen. Anything else - a script piping
    answers in, a job on a build machine - gets them one at a time, and
    `--plain` asks for that on purpose.
    """
    if plain or not shared._has_a_screen():
        answers = _setup_questions()
    else:
        # Imported here so the command line stays quick for everything that
        # never opens a screen.
        from warden.wizard import run

        chosen = run()
        if chosen is None:
            console.print("Nothing written.", style=theme.BONE_DIM)
            return
        answers = chosen

    written = config.write(answers)
    console.print()
    console.print(f"Written to {written}", style=theme.MOSS)
    console.print("`warden settings` shows what is in effect.", style=theme.BONE_DIM)
    # A warden reads its settings once, when it starts. Writing a new webhook
    # into the file and watching nothing arrive is otherwise a puzzling hour.
    console.print(
        "A warden that is already running keeps the settings it started with.",
        style=theme.BONE_DIM,
    )


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
    if not shared._has_a_screen():
        raise _fail(
            WardenError(
                "this terminal cannot draw a screen; `warden ls` and `warden ports` "
                "read the same things in plain text"
            )
        )
    from warden.tui import run

    run(url, token=token, interval=interval, fleet=every)


def _say_notes(starter: autostart.Autostart) -> None:
    """Said out loud, because it is the difference between installed and working."""
    for note in starter.notes():
        errors.print(note, style=theme.SHRIEKER)


def _plan_lines(plan: autostart.Plan) -> list[str]:
    """Everything installing would write or run, so it can be read first."""
    lines = [str(plan.path)] if plan.path else []
    if plan.body:
        lines.extend(plan.body.strip().splitlines())
    return lines + plan.steps


service_app = typer.Typer(help="Start warden with the machine.")
app.add_typer(service_app, name="service")


@service_app.command("install")
def service_install(
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Write it without asking.")
    ] = False,
) -> None:
    """Have this machine start `warden serve` at login.

    As the account running this command, never as root or SYSTEM: a warden
    started by another user reads another user's settings and hands out ports
    from a registry nobody else can see.
    """
    try:
        starter = autostart.autostart_for()
    except WardenError as exc:
        raise _fail(exc) from exc

    plan = starter.plan()
    console.print(plan.kind, style=theme.GLOW)
    for line in _plan_lines(plan):
        console.print(f"  {line}", style=theme.BONE_DIM, highlight=False)

    if not yes and not typer.confirm("Write it?"):
        console.print("nothing written", style=theme.BONE_DIM)
        return
    try:
        starter.install()
    except WardenError as exc:
        raise _fail(exc) from exc
    console.print(f"warden starts at login - {starter.status()}")
    _say_notes(starter)


@service_app.command("uninstall")
def service_uninstall(
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Remove it without asking.")
    ] = False,
) -> None:
    """Stop starting warden at login and remove what was written."""
    try:
        starter = autostart.autostart_for()
    except WardenError as exc:
        raise _fail(exc) from exc

    if starter.status() == autostart.MISSING:
        console.print("warden does not start at login here", style=theme.BONE_DIM)
        return
    if not yes and not typer.confirm(f"Remove the {starter.kind}?"):
        console.print("left alone", style=theme.BONE_DIM)
        return
    try:
        starter.uninstall()
    except WardenError as exc:
        raise _fail(exc) from exc
    console.print("warden no longer starts at login")


@service_app.command("status")
def service_status(as_json: JsonOption = False) -> None:
    """Say whether warden starts at login, and whether it is up now."""
    try:
        starter = autostart.autostart_for()
        state = starter.status()
    except WardenError as exc:
        raise _fail(exc) from exc

    notes = starter.notes()
    if as_json:
        _dump({"kind": starter.kind, "status": state, "notes": notes})
        return
    colour = theme.MOSS if state == autostart.RUNNING else theme.BONE_DIM
    console.print(Text(state, style=colour), f"({starter.kind})")
    _say_notes(starter)


@app.command()
def doctor(
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Check everything at once and say what is wrong.

    Exits 1 only when something failed, so a warning about an unset token does
    not make a health check call the machine down.
    """
    with shared._client(url, token) as client:
        checks = health.examine(client, Settings())

    if as_json:
        _dump([{"level": check.level, "text": check.text} for check in checks])
    else:
        # A table, so a line that wraps keeps its second half under the text
        # rather than under the level it belongs to.
        report = Table(box=None, pad_edge=False, show_header=False)
        report.add_column(no_wrap=True)
        report.add_column(overflow="fold")
        for check in checks:
            report.add_row(
                Text(check.level, style=health.LEVEL_COLOURS[check.level]), check.text
            )
        console.print(report)
    raise typer.Exit(health.exit_code(checks))

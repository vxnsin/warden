"""The other wardens, and keeping them current."""

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
)
from warden.core import updates
from warden.core.config import Settings
from warden.errors import WardenError
from warden.models import (
    FleetUpdate,
    UpdateStatus,
)

ORDER = 70


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
    """Say whether a newer warden exists, and optionally go and get it.

    Whether a newer warden exists is a question about this installation, so it
    is answered with or without one running. A warden that is up has the answer
    already; without one, this asks GitHub itself. Only `--fleet` needs a hub.
    """
    settings = Settings()
    with shared._client(url, token) as client:
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
            return
        except WardenError as exc:
            if fleet or not _nobody_home(exc):
                raise _fail(exc) from exc

    # No warden answered, and the question was never really about one.
    try:
        if not apply:
            _show_update(updates.check_now(settings), as_json=as_json)
            return
        if not yes and not typer.confirm("Update this machine?"):
            console.print("left alone", style=theme.BONE_DIM)
            return
        detail = updates.run_here(settings)
    except WardenError as exc:
        raise _fail(exc) from exc
    _dump({"detail": detail}) if as_json else console.print(detail)


def _nobody_home(exc: WardenError) -> bool:
    """Whether this was 'no warden there' rather than a warden saying no."""
    return "no warden reachable" in exc.message

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
    with shared._client(url, token) as client:
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

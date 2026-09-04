"""Following what happens, and where it is posted."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from warden import theme
from warden.cli import shared
from warden.cli.shared import (
    ACTION_COLOURS,
    JsonOption,
    TokenOption,
    UrlOption,
    _dump,
    _fail,
    app,
    console,
)
from warden.core.config import Settings
from warden.core.events import redacted
from warden.errors import WardenError

ORDER = 40


@app.command()
def events(
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Follow what happens, as it happens.

    Runs until it is stopped, which is what makes it worth piping somewhere.
    With `--json` that is one event per line, flushed as it arrives.
    """
    with shared._client(url, token) as client:
        try:
            for event in client.events():
                if as_json:
                    print(json.dumps(event.model_dump(mode="json"), default=str), flush=True)
                    continue
                line = Text(f"{event.at.astimezone():%H:%M:%S}  ", style=theme.BONE_DIM)
                line.append(
                    f"{event.action:<11}",
                    style=ACTION_COLOURS.get(event.action, theme.BONE),
                )
                line.append(f"{event.name}  ")
                line.append(event.address, style=theme.BONE_DIM)
                console.print(line)
        except WardenError as exc:
            raise _fail(exc) from exc
        except KeyboardInterrupt:
            # Stopping a stream on purpose is not an error worth a traceback.
            pass


@app.command()
def webhook(
    test: Annotated[
        bool, typer.Option("--test", help="Post one made-up event from this machine now.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Where events are posted, and whether that is working.

    `--test` posts from here with this machine's settings, which is what
    `warden setup` has just written down. Without it the answer comes from the
    warden that is running, which is a different thing and can differ.
    """
    if test:
        here = Settings()
        if not here.webhook:
            raise _fail(WardenError("nothing to post to - `warden setup` writes one down"))
        problem = shared.send_one(here)
        if as_json:
            _dump({"target": redacted(here.webhook), "posted": not problem, "error": problem})
        elif problem:
            raise _fail(WardenError(f"it did not arrive: {problem}"))
        else:
            console.print(f"posted to {redacted(here.webhook)}", style=theme.MOSS)
        return

    with shared._client(url, token) as client:
        try:
            status = client.webhook()
        except WardenError as exc:
            raise _fail(exc) from exc

    if as_json:
        _dump(status.model_dump(mode="json"))
        return
    if not status.configured:
        console.print("this warden posts events nowhere", style=theme.BONE_DIM)
        return

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(no_wrap=True, style=theme.BONE_DIM)
    table.add_column(overflow="fold")
    table.add_row("address", status.target)
    table.add_row("shape", status.format)
    table.add_row("events", theme.listed(status.actions))
    table.add_row("delivered", str(status.delivered))
    if status.failed:
        table.add_row("never arrived", str(status.failed))
    if status.dropped:
        table.add_row("dropped", str(status.dropped))
    if status.last_sent:
        table.add_row("last sent", theme.age(status.last_sent))
    if status.last_error:
        table.add_row("last error", Text(status.last_error, style=theme.EMBER))
    console.print(table)

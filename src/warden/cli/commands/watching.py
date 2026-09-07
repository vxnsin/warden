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
from warden.core import happenings
from warden.core.config import Settings
from warden.core.events import redacted
from warden.errors import WardenError

ORDER = 40


@app.command("events")
def follow_events(
    known: Annotated[
        bool, typer.Option("--known", help="List everything warden can tell you about.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Follow what happens, as it happens.

    Runs until it is stopped, which is what makes it worth piping somewhere.
    With `--json` that is one event per line, flushed as it arrives.
    """
    if known:
        _say_what_can_happen(as_json)
        return
    with shared._client(url, token) as client:
        try:
            for event in client.events():
                if as_json:
                    print(json.dumps(event.model_dump(mode="json"), default=str), flush=True)
                    continue
                line = Text(f"{event.at.astimezone():%H:%M:%S}  ", style=theme.BONE_DIM)
                line.append(
                    f"{event.full:<20}",
                    style=ACTION_COLOURS.get(event.action, theme.BONE),
                )
                line.append(f"{event.subject}  ")
                line.append(
                    event.address if event.scope == "port" else _shortly(event.body),
                    style=theme.BONE_DIM,
                )
                console.print(line)
        except WardenError as exc:
            raise _fail(exc) from exc
        except KeyboardInterrupt:
            # Stopping a stream on purpose is not an error worth a traceback.
            pass


def _shortly(body: dict[str, object]) -> str:
    return "  ".join(f"{key}={value}" for key, value in body.items())


def _say_what_can_happen(as_json: bool) -> None:
    """The whole list, so nobody has to guess what to put in webhook_events."""
    if as_json:
        _dump(
            [
                {"name": one.full, "means": one.means, "by_default": one.notable}
                for one in happenings.EVERY
            ]
        )
        return
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("EVENT", "MEANS", "POSTED"):
        table.add_column(column)
    for one in happenings.EVERY:
        table.add_row(
            Text(one.full, style=theme.GLOW),
            one.means,
            Text("yes", style=theme.MOSS) if one.notable else Text("ask", style=theme.BONE_DIM),
        )
    console.print(table)
    console.print(
        "A whole scope works too: firewall.* - and the old bare names still do.",
        style=theme.BONE_DIM,
    )

@app.command()
def webhook(
    test: Annotated[
        bool, typer.Option("--test", help="Post one made-up event from this machine now.")
    ] = False,
    event: Annotated[
        str | None,
        typer.Option("--event", help="Which one to make up, like `node.stale`."),
    ] = None,
    every: Annotated[
        bool, typer.Option("--all", help="Post one of every event there is, in order.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Where events are posted, and whether that is working.

    `--test` posts from here with this machine's settings, which is what
    `warden setup` has just written down. Without it the answer comes from the
    warden that is running, which is a different thing and can differ.

    `--event node.stale` makes up that one instead, and `--all` posts one of
    each - which is how to see what thirteen kinds of message look like in a
    chat window without waiting for thirteen things to happen.
    """
    if test or every or event:
        _post_made_up(_which(event, every), as_json=as_json)
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


def _which(event: str | None, every: bool) -> list[str]:
    """The events to make up, in the order they are catalogued."""
    if every:
        return list(happenings.NAMES)
    if event is None:
        return ["port.registered"]
    full = happenings.known(event)
    if full is None:
        raise _fail(
            WardenError(f"no event called {event!r}; there is {', '.join(happenings.NAMES)}")
        )
    return [full]


def _post_made_up(names: list[str], *, as_json: bool) -> None:
    """Post one made-up event per name and say what arrived.

    Posted one at a time and in order, because a chat window shows them in the
    order they land and thirteen at once would land in whichever order the
    other end felt like.
    """
    here = Settings()
    if not here.webhook:
        raise _fail(WardenError("nothing to post to - `warden setup` writes one down"))

    sent = [(name, shared.send_one(here, happenings.like(name))) for name in names]
    if len(sent) == 1:
        _one_went(here.webhook, sent[0][1], as_json=as_json)
        return

    if as_json:
        _dump(
            {
                "target": redacted(here.webhook),
                "posted": [
                    {"event": name, "posted": not problem, "error": problem}
                    for name, problem in sent
                ],
            }
        )
        return

    for name, problem in sent:
        console.print(
            f"{name:<22}{problem or 'posted'}",
            style=theme.EMBER if problem else theme.MOSS,
        )
    console.print(f"to {redacted(here.webhook)}", style=theme.BONE_DIM)
    if any(problem for _, problem in sent):
        raise typer.Exit(1)


def _one_went(webhook: str, problem: str | None, *, as_json: bool) -> None:
    """One event, said the way it has always been said."""
    if as_json:
        _dump({"target": redacted(webhook), "posted": not problem, "error": problem})
    elif problem:
        raise _fail(WardenError(f"it did not arrive: {problem}"))
    else:
        console.print(f"posted to {redacted(webhook)}", style=theme.MOSS)

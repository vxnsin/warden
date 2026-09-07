"""Rules for what may cross."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
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
from warden.core import store
from warden.core.config import Settings
from warden.errors import WardenError
from warden.firewall import adopt, catalogue, guard, link
from warden.firewall import model as firewall
from warden.firewall.backends import base
from warden.fleet import aggregate
from warden.models import Unreachable

ORDER = 50


firewall_app = typer.Typer(help="Rules for what may cross.")
app.add_typer(firewall_app, name="firewall")

RULE_ORIGINS = {
    firewall.Origin.MANUAL: theme.BONE,
    firewall.Origin.ADOPTED: theme.AMETHYST,
    firewall.Origin.REGISTRY: theme.GLOW,
    firewall.Origin.CATALOGUE: theme.BONE_DIM,
}

ACTION_STYLES = {
    firewall.Action.ALLOW: theme.MOSS,
    firewall.Action.DENY: theme.EMBER,
    firewall.Action.REJECT: theme.SHRIEKER,
}


def _rules() -> store.RuleStore:
    """The rule store on this machine's database, without a server in between."""
    return store.RuleStore(store.Store(Settings().database))


def _rate(rule: firewall.Rule) -> Text:
    return (
        Text(rule.limit, style=theme.AMETHYST)
        if rule.limit
        else Text("-", style=theme.BONE_DIM)
    )


def _what(rule: firewall.Rule) -> str:
    if rule.protocol in (firewall.Protocol.ICMP, firewall.Protocol.ANY):
        return str(rule.protocol)
    known = catalogue.named(rule.protocol, rule.ports)
    ports = firewall.spelled(rule.ports) or "any"
    return f"{rule.protocol}/{ports}" + (f" ({known})" if known else "")


def _rules_table(rules: list[firewall.Rule]) -> Table:
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("NAME", "DIR", "ACTION", "WHAT", "FROM", "RATE", "ORIGIN", "UNTIL"):
        table.add_column(column)
    for rule in rules:
        table.add_row(
            Text(rule.name, style="" if rule.enabled else theme.BONE_DIM),
            str(rule.direction),
            Text(str(rule.action), style=ACTION_STYLES[rule.action]),
            _what(rule),
            rule.source,
            _rate(rule),
            Text(str(rule.origin), style=RULE_ORIGINS[rule.origin]),
            Text(theme.until(rule.expires_at), style=theme.SHRIEKER)
            if rule.expires_at
            else Text("-", style=theme.BONE_DIM),
        )
    return table


def _fleet_rules_table(rules: list[dict[str, object]]) -> Table:
    """The same rules with the node they are on, and no model in between.

    A hub lists a fleet that may be running a newer warden than itself, so a
    rule is shown as it arrived rather than parsed into a shape this version
    happens to know.
    """
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in (
        "NODE", "NAME", "DIR", "ACTION", "WHAT", "FROM", "RATE", "ORIGIN", "UNTIL"
    ):
        table.add_column(column)
    for rule in rules:
        action = str(rule.get("action", ""))
        origin = str(rule.get("origin", ""))
        table.add_row(
            Text(str(rule.get("node", "")), style=theme.AMETHYST),
            str(rule.get("name", "")),
            str(rule.get("direction", "")),
            Text(action, style=ACTION_STYLES.get(action, "")),
            _spelled(rule),
            str(rule.get("source", "")),
            Text(
                str(rule.get("limit") or "-"),
                style=theme.AMETHYST if rule.get("limit") else theme.BONE_DIM,
            ),
            Text(origin, style=RULE_ORIGINS.get(origin, "")),
            _until(rule.get("expires_at")),
        )
    return table


def _spelled(rule: dict[str, object]) -> str:
    """What a rule is about, from the fields rather than from a model."""
    return catalogue.describe(str(rule.get("protocol", "")), rule.get("ports") or [])


def _until(said: object) -> Text:
    if not said:
        return Text("-", style=theme.BONE_DIM)
    with suppress(ValueError):
        return Text(theme.until(datetime.fromisoformat(str(said))), style=theme.SHRIEKER)
    return Text(str(said))


def _missing(unreachable: list[Unreachable]) -> None:
    for node in unreachable:
        errors.print(f"{node.node} ({node.url}) {node.reason}", style=theme.SHRIEKER)


def _tidy(url: str | None = None, token: str | None = None) -> list[str]:
    """Close every rule whose service is gone, before anything reads them.

    A rule outliving its registration is the failure this whole link was
    designed against, so it is checked wherever the rules are looked at rather
    than only where they are written.
    """
    rules = _rules()
    held = rules.list()
    if not any(rule.origin is firewall.Origin.REGISTRY for rule in held):
        return []
    try:
        with shared._client(url, token) as client:
            services = client.services()
    except WardenError:
        # No warden to ask. Saying nothing is right: a rule is not stale just
        # because the registry is not answering this minute.
        return []
    stale = link.reconcile(held, services)
    rules.delete_many(stale)
    return stale


def _asking(url: str | None, token: str | None, ask):
    """Put a question to a warden, and fail the way every command does.

    No address means the one this machine is configured to talk to, which for
    a fleet question is the hub.
    """
    try:
        with shared._client(url, token) as client:
            return ask(client)
    except WardenError as exc:
        raise _fail(exc) from exc


def _said_closed(closed: list[str]) -> None:
    for name in closed:
        errors.print(f"closed {name} - its service is gone", style=theme.BONE_DIM)


@firewall_app.command("list")
def firewall_list(
    origin: Annotated[
        str | None, typer.Option(help="Only rules that came from here.")
    ] = None,
    every: Annotated[
        bool, typer.Option("--all", help="Every warden in the fleet, not just this one.")
    ] = False,
    on: Annotated[
        str | None, typer.Option("--on", help="Ask the warden at this address instead.")
    ] = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Every rule this machine holds, and where each one came from.

    `--all` asks the whole fleet through the hub and names the nodes that did
    not answer. `--on http://host:7010` asks one warden by address instead.
    Neither needs anything switched on there - reading is what a token already
    allows.
    """
    if every:
        _list_the_fleet(url, token, origin=origin, as_json=as_json)
        return
    if on:
        rules = _asking(on, token, lambda client: client.firewall_rules(origin=origin))
    else:
        _said_closed(_tidy())
        rules = _rules().list(origin=origin)
    if as_json:
        _dump([rule.model_dump(mode="json") for rule in rules])
        return
    if not rules:
        console.print("no rules yet", style=theme.BONE_DIM)
        return
    console.print(_rules_table(rules))


def _list_the_fleet(
    url: str | None, token: str | None, *, origin: str | None, as_json: bool
) -> None:
    """Every rule anywhere, with the node it is on."""
    found = _asking(url, token, lambda client: client.fleet_firewall_rules(origin=origin))
    if as_json:
        _dump(found.model_dump(mode="json"))
        return
    if found.rules:
        console.print(_fleet_rules_table(found.rules))
    else:
        console.print("no rules anywhere", style=theme.BONE_DIM)
    _missing(found.unreachable)


def _rule_from(
    what: str,
    *,
    action: firewall.Action,
    source: str,
    direction: firewall.Direction,
    protocol: str | None,
    comment: str | None,
    limit: str | None = None,
) -> firewall.Rule:
    """A port, a port range, or a name out of the catalogue."""
    return catalogue.rule_for(
        what,
        action=action,
        source=source,
        direction=direction,
        protocol=protocol,
        comment=comment,
        limit=limit,
    )


def _write(rule: firewall.Rule, as_json: bool) -> None:
    _rules().save(rule)
    if as_json:
        _dump(rule.model_dump(mode="json"))
        return
    console.print(_rules_table([rule]))
    console.print(
        "written down, not applied - `warden firewall export` shows what it would become",
        style=theme.BONE_DIM,
    )


@firewall_app.command("allow")
def firewall_allow(
    what: Annotated[str, typer.Argument(help="A port, or a name like ssh.")],
    source: Annotated[
        str, typer.Option("--from", help="Only from this address or network.")
    ] = "any",
    direction: Annotated[str, typer.Option(help="in or out.")] = "in",
    protocol: Annotated[str | None, typer.Option(help="tcp, udp, icmp or any.")] = None,
    comment: Annotated[str | None, typer.Option(help="Why this rule exists.")] = None,
    limit: Annotated[
        str | None,
        typer.Option(help="How often it may happen: 10/second, 6/minute."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Let something through."""
    try:
        rule = _rule_from(
            what,
            action=firewall.Action.ALLOW,
            source=source,
            direction=firewall.Direction(direction),
            protocol=protocol,
            comment=comment,
            limit=limit,
        )
    except (WardenError, ValueError) as exc:
        raise _fail(WardenError(str(getattr(exc, "message", exc)))) from exc
    _write(rule, as_json)


@firewall_app.command("deny")
def firewall_deny(
    what: Annotated[str, typer.Argument(help="A port, or a name like ssh.")],
    source: Annotated[
        str, typer.Option("--from", help="Only from this address or network.")
    ] = "any",
    direction: Annotated[str, typer.Option(help="in or out.")] = "in",
    protocol: Annotated[str | None, typer.Option(help="tcp, udp, icmp or any.")] = None,
    comment: Annotated[str | None, typer.Option(help="Why this rule exists.")] = None,
    limit: Annotated[
        str | None,
        typer.Option(help="How often it may happen: 10/second, 6/minute."),
    ] = None,
    reject: Annotated[
        bool, typer.Option("--reject", help="Answer instead of saying nothing.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Keep something out."""
    try:
        rule = _rule_from(
            what,
            action=firewall.Action.REJECT if reject else firewall.Action.DENY,
            source=source,
            direction=firewall.Direction(direction),
            protocol=protocol,
            comment=comment,
            limit=limit,
        )
    except (WardenError, ValueError) as exc:
        raise _fail(WardenError(str(getattr(exc, "message", exc)))) from exc
    _write(rule, as_json)


@firewall_app.command("open")
def firewall_open(
    service: Annotated[str, typer.Argument(help="A name the registry knows.")],
    source: Annotated[
        str, typer.Option("--from", help="Which network may reach it.")
    ] = "",
    every: Annotated[
        bool, typer.Option("--all", help="Every node in the fleet that holds it.")
    ] = False,
    at: Annotated[
        str | None,
        typer.Option("--node", help="Ask that node in the fleet, through the hub."),
    ] = None,
    on: Annotated[
        str | None,
        typer.Option("--on", help="Ask the warden at this address to open it there."),
    ] = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Open the port a registered service actually holds.

    The registry knows which port that is and how long the service has it for,
    so the rule inherits both. Nothing is opened by registering: this is a
    person asking, and it is bounded by what the registry may ever open.

    `--node build-01` goes through the hub to that node; `--on http://host:7010`
    asks one warden by address. Either way it is the machine being asked that
    decides, and it will only do it if allow_remote_firewall is set there.
    """
    if every:
        _opened_everywhere(url, token, service, source=source, as_json=as_json)
        return
    if at:
        opened = _asking(
            url, token, lambda client: client.firewall_open_on(at, service, source=source)
        )
        if as_json:
            _dump(opened)
        else:
            console.print(_fleet_rules_table([opened]))
        return
    if on:
        rule = _asking(on, token, lambda client: client.firewall_open(service, source=source))
        if as_json:
            _dump(rule.model_dump(mode="json"))
        else:
            console.print(_rules_table([rule]))
        return

    settings = Settings()
    try:
        with shared._client(url, token) as client:
            known = link.found(client.services(), service)
        rule = link.rule_for(
            known,
            source=source or _only_network(settings),
            settings=settings,
        )
    except WardenError as exc:
        raise _fail(exc) from exc
    _write(rule, as_json)


def _only_network(settings: Settings) -> str:
    """The one declared network, when there is exactly one to mean."""
    allowed = sorted(settings.firewall_allow_from)
    if len(allowed) == 1:
        return allowed[0]
    return "any"  # bounds refuses this, and says which networks are declared


@firewall_app.command("dev-mode")
def firewall_dev_mode(
    source: Annotated[
        str, typer.Option("--from", help="Which network may reach the pool.")
    ] = "",
    hours: Annotated[
        float, typer.Option("--for", help="How many hours it stays open.")
    ] = 2.0,
    as_json: JsonOption = False,
) -> None:
    """Open the whole pool for a while, and close it again on its own.

    For the afternoon somebody else needs to reach what you are running. It
    cannot reach a port warden does not hand out, it says when it ends, and it
    ends whether or not anyone remembers.
    """
    settings = Settings()
    try:
        rule = link.window(
            settings, source or _only_network(settings), int(hours * 3600)
        )
    except WardenError as exc:
        raise _fail(exc) from exc
    _write(rule, as_json)
    if not as_json:
        console.print(
            f"closes on its own at {rule.expires_at:%H:%M}",
            style=theme.SHRIEKER,
        )


@firewall_app.command("delete")
def firewall_delete(
    name: Annotated[str, typer.Argument(help="The rule to remove.")],
    at: Annotated[
        str | None,
        typer.Option("--node", help="Take it out on that node, through the hub."),
    ] = None,
    url: UrlOption = None,
    token: TokenOption = None,
) -> None:
    """Take a rule away."""
    if at:
        _asking(url, token, lambda client: client.firewall_close_on(at, name))
        console.print(f"removed {name} on {at}", style=theme.BONE_DIM)
        return
    if not _rules().delete(name):
        raise _fail(WardenError(f"no rule called {name!r}"))
    console.print(f"removed {name}", style=theme.BONE_DIM)


@firewall_app.command("export")
def firewall_export(
    shape: Annotated[
        str | None, typer.Option("--for", help="Which firewall to write for.")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Show the policy in the words of a firewall.

    It prints and stops - nothing changes anywhere. `--for` writes for a
    firewall this machine does not have, which is how a ruleset gets read on a
    laptop before it reaches the machine it is meant for.
    """
    rules = _rules().list()
    policy = firewall.Policy(rules=rules)
    try:
        backend = base.backend_for(shape) if shape else _backend()
    except WardenError as exc:
        raise _fail(
            WardenError(f"{exc.message} - name one with --for" if not shape else exc.message)
        ) from exc
    if as_json:
        _dump(policy.model_dump(mode="json"))
        return
    print(backend.render(policy), end="")
    if not backend.available():
        errors.print(
            f"no {backend.kind} on this machine - this is what it would say",
            style=theme.BONE_DIM,
        )


def _backend() -> base.Backend:
    """Whichever one this machine uses, or the one the settings name."""
    return base.backend_for(Settings().firewall_backend)


def _snapshots() -> store.Snapshots:
    return store.Snapshots(store.Store(Settings().database))


def _waiting_line(waiting: guard.Armed) -> None:
    console.print(
        f"rolling back in {waiting.left():.0f}s unless you run `warden firewall confirm`",
        style=theme.SHRIEKER,
    )


# What a node did, said the way somebody would say it.
DONE = {"apply": "applied", "confirm": "confirmed", "restore": "restored"}


def _opened_everywhere(
    url: str | None, token: str | None, service: str, *, source: str, as_json: bool
) -> None:
    """One line per node: opened, skipped for not holding it, or refused."""
    found = _asking(
        url, token, lambda client: client.firewall_open_everywhere(service, source=source)
    )
    if as_json:
        _dump(found.model_dump(mode="json"))
        return

    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("NODE", "RESULT", "DETAIL"):
        table.add_column(column, overflow="fold" if column == "DETAIL" else None)
    for result in found.results:
        skipped = not result.ok and result.detail.startswith("does not hold")
        table.add_row(
            Text(result.node, style=theme.AMETHYST),
            Text(
                "opened" if result.ok else ("skipped" if skipped else "refused"),
                style=theme.MOSS
                if result.ok
                else (theme.BONE_DIM if skipped else theme.EMBER),
            ),
            result.detail,
        )
    console.print(table)
    console.print()
    console.print(
        f"{found.kept} opened, written down and not applied - "
        "`warden firewall apply --fleet` makes them true",
        style=theme.BONE_DIM,
    )


def _across_the_fleet(
    url: str | None,
    token: str | None,
    what: str,
    *,
    rollback: int | None = None,
    as_json: bool,
) -> None:
    """One line per node, whichever way each of them went."""
    if what == "apply":
        found = _asking(url, token, lambda client: client.firewall_apply_fleet(rollback=rollback))
    elif what == "confirm":
        found = _asking(url, token, lambda client: client.firewall_confirm_fleet())
    else:
        found = _asking(url, token, lambda client: client.firewall_restore_fleet())

    if as_json:
        _dump(found.model_dump(mode="json"))
        return

    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("NODE", "RESULT", "DETAIL"):
        table.add_column(column, overflow="fold" if column == "DETAIL" else None)
    for result in found.results:
        table.add_row(
            Text(result.node, style=theme.AMETHYST),
            Text(
                DONE[what] if result.ok else "refused",
                style=theme.MOSS if result.ok else theme.EMBER,
            ),
            result.detail,
        )
    console.print(table)

    kept = found.kept
    console.print()
    console.print(f"{kept} of {len(found.results)} {DONE[what]}", style=theme.BONE_DIM)
    if what == "apply" and kept:
        console.print(
            "`warden firewall confirm --fleet` keeps them; anything not confirmed "
            "puts itself back",
            style=theme.SHRIEKER,
        )
    if kept < len(found.results):
        raise typer.Exit(1)


@firewall_app.command("apply")
def firewall_apply(
    rollback: Annotated[
        int | None,
        typer.Option(help="Seconds to wait for a confirmation. 0 turns it off."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
    fleet: Annotated[
        bool, typer.Option("--fleet", help="Every warden in the fleet, each on its own.")
    ] = False,
    at: Annotated[
        str | None, typer.Option("--node", help="One node in the fleet, through the hub.")
    ] = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Make the rules true on this machine.

    A snapshot is taken first and a rollback armed, so a rule that locks you
    out undoes itself rather than needing somebody at the keyboard.

    `--fleet` asks every node to do the same to its own rules, and refuses to
    do it without a rollback: one wrong rule would otherwise shut every machine
    at once. `--node build-01` asks exactly one.
    """
    if fleet:
        _across_the_fleet(url, token, "apply", rollback=rollback, as_json=as_json)
        return
    if at:
        said = _asking(url, token, lambda client: client.firewall_apply_on(at, rollback=rollback))
        _dump(said) if as_json else console.print(f"{at}: {aggregate._said('apply', said)}")
        return

    settings = Settings()
    seconds = settings.firewall_rollback if rollback is None else rollback
    policy = firewall.Policy(rules=_rules().list())
    try:
        backend = _backend()
    except WardenError as exc:
        raise _fail(exc) from exc

    if not yes:
        console.print(backend.render(policy), end="", highlight=False)
        if not typer.confirm("Apply this?"):
            console.print("left alone", style=theme.BONE_DIM)
            return

    snapshots = _snapshots()
    try:
        waiting = guard.apply(backend, snapshots, policy, rollback=seconds)
    except WardenError as exc:
        raise _fail(exc) from exc

    if waiting is not None:
        guard.start_watchdog(str(settings.database), waiting.deadline)
    if as_json:
        _dump(
            {
                "applied": True,
                "rollback_at": waiting.deadline.isoformat() if waiting else None,
            }
        )
        return
    console.print(f"{len(policy.live(datetime.now(UTC)))} rules applied", style=theme.MOSS)
    if waiting is None:
        console.print("no rollback armed", style=theme.BONE_DIM)
    else:
        _waiting_line(waiting)


@firewall_app.command("adopt")
def firewall_adopt(
    manager: Annotated[
        str | None, typer.Option(help="Which firewall to read. Found if not given.")
    ] = None,
    rollback: Annotated[
        int | None, typer.Option(help="Seconds to wait for a confirmation. 0 turns it off.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Take over from the firewall that is managing this machine now.

    Reads its rules first, shows them, applies them as warden's own, and only
    turns the other one off once you have confirmed. Until then it is still
    enabled, so rolling back returns the machine exactly as it was.
    """
    settings = Settings()
    found = [manager] if manager else adopt.managing()
    if not found:
        console.print("nothing else is managing this machine", style=theme.BONE_DIM)
        return

    taking = found[0]
    reading = adopt.read(taking)
    if as_json:
        _dump(
            {
                "manager": taking,
                "rules": [rule.model_dump(mode="json") for rule in reading.rules],
                "untranslated": reading.untranslated,
            }
        )
        return

    console.print(f"{taking} is holding {theme.plural(len(reading.rules), 'rule')}")
    console.print(_rules_table(reading.rules))
    for line in reading.untranslated:
        errors.print(f"could not read: {line}", style=theme.SHRIEKER)
    if reading.untranslated:
        errors.print(
            "those would be lost. Write them by hand first, or say no.",
            style=theme.SHRIEKER,
        )

    if not yes and not typer.confirm(f"Take over from {taking}?"):
        console.print("left alone", style=theme.BONE_DIM)
        return

    rules = _rules()
    rules.save_many(reading.rules)
    seconds = settings.firewall_rollback if rollback is None else rollback
    policy = firewall.Policy(rules=rules.list())
    try:
        backend = _backend()
        waiting = guard.apply(
            backend, _snapshots(), policy, rollback=seconds, reason=f"{guard.ADOPTING}:{taking}"
        )
    except WardenError as exc:
        rules.delete_many([rule.name for rule in reading.rules])
        raise _fail(exc) from exc

    if waiting is not None:
        guard.start_watchdog(str(settings.database), waiting.deadline)
    console.print(f"{len(policy.rules)} rules applied", style=theme.MOSS)
    console.print(
        f"{taking} is still enabled and its rules are not loaded - confirming turns "
        f"it off, restoring puts it back",
        style=theme.BONE_DIM,
    )
    drifted = guard.pending(_rules().list(), _snapshots())
    if drifted:
        console.print(
            f"{theme.plural(drifted.count, 'rule')} changed since the last apply"
            " - `warden firewall pending` says which",
            style=theme.SHRIEKER,
        )
    if waiting is not None:
        _waiting_line(waiting)


@firewall_app.command("confirm")
def firewall_confirm(
    fleet: Annotated[
        bool, typer.Option("--fleet", help="Every warden in the fleet, each on its own.")
    ] = False,
    at: Annotated[
        str | None, typer.Option("--node", help="One node in the fleet, through the hub.")
    ] = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Keep what was applied, and call off the rollback.

    `--fleet` keeps it everywhere; any node left unconfirmed still puts itself
    back when its own window runs out.
    """
    if fleet:
        _across_the_fleet(url, token, "confirm", as_json=as_json)
        return
    if at:
        said = _asking(url, token, lambda client: client.firewall_confirm_on(at))
        _dump(said) if as_json else console.print(f"{at}: kept", style=theme.MOSS)
        return

    try:
        kept = guard.confirm(_snapshots())
    except WardenError as exc:
        raise _fail(exc) from exc
    console.print("kept", style=theme.MOSS)

    # Only now: until this moment a rollback could have put the machine back
    # exactly as it was, other firewall and all.
    taken = (kept.reason or "").partition(f"{guard.ADOPTING}:")[2]
    if taken:
        for step in adopt.stand_down(taken):
            console.print(f"  {step}", style=theme.BONE_DIM)
        console.print(f"{taken} is off", style=theme.MOSS)


@firewall_app.command("restore")
def firewall_restore(
    snapshot: Annotated[
        int | None, typer.Argument(help="Which snapshot. The last one by default.")
    ] = None,
) -> None:
    """Put the firewall back the way it was."""
    try:
        which = guard.roll_back(_backend(), _snapshots(), snapshot)
    except WardenError as exc:
        raise _fail(exc) from exc
    console.print(f"restored snapshot {which}", style=theme.MOSS)


def _status_of_the_fleet(url: str | None, token: str | None, *, as_json: bool) -> None:
    """One line per node: what it runs, what it holds, what is about to undo itself."""
    found = _asking(url, token, lambda client: client.fleet_firewall())
    if as_json:
        _dump(found.model_dump(mode="json"))
        return

    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("NODE", "BACKEND", "RULES", "REGISTRY", "PENDING", "REMOTE", "ROLLING BACK"):
        table.add_column(column)
    for one in found.firewalls:
        table.add_row(
            Text(one.node, style=theme.AMETHYST),
            Text(one.backend, style="" if one.available else theme.BONE_DIM),
            str(one.rules),
            str(one.from_registry),
            Text(str(one.pending), style=theme.SHRIEKER)
            if one.pending
            else Text("-", style=theme.BONE_DIM),
            Text("yes", style=theme.MOSS) if one.remote else Text("no", style=theme.BONE_DIM),
            Text(theme.until(one.rollback_at), style=theme.SHRIEKER)
            if one.rollback_at
            else Text("-", style=theme.BONE_DIM),
        )
    console.print(table)
    _missing(found.unreachable)


@firewall_app.command("pending")
def firewall_pending(as_json: JsonOption = False) -> None:
    """Which rules have changed since the last apply.

    Writing a rule down does not make it true, and nothing said so again after
    the moment it was written. On a machine where three rules have been in the
    book since yesterday, this is the difference between a firewall and a list.
    """
    _said_closed(_tidy())
    found = guard.pending(_rules().list(), _snapshots())

    if as_json:
        _dump(
            {
                "added": found.added,
                "removed": found.removed,
                "applied_at": found.applied_at.isoformat() if found.applied_at else None,
                "unknown": found.unknown,
            }
        )
        return

    if found.unknown:
        console.print(
            "nothing has been applied from here, so there is nothing to compare "
            "against - `warden firewall apply` makes what is written down true",
            style=theme.SHRIEKER,
        )
        if found.added:
            console.print(f"{theme.plural(len(found.added), 'rule')} written down",
                          style=theme.BONE_DIM)
        return

    if not found:
        console.print(
            f"nothing since the last apply, {theme.age(found.applied_at)}",
            style=theme.MOSS,
        )
        return

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    for name in found.added:
        table.add_row(Text("+", style=theme.MOSS), name)
    for name in found.removed:
        table.add_row(Text("-", style=theme.EMBER), name)
    console.print(table)
    console.print()
    console.print(
        f"last applied {theme.age(found.applied_at)}  -  "
        "`warden firewall apply` makes them true",
        style=theme.BONE_DIM,
    )


@firewall_app.command("status")
def firewall_status(
    every: Annotated[
        bool, typer.Option("--all", help="Every warden in the fleet, not just this one.")
    ] = False,
    on: Annotated[
        str | None, typer.Option("--on", help="Ask the warden at this address instead.")
    ] = None,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Whether a rollback is waiting, and what this machine can do.

    `--all` asks the whole fleet through the hub, one line per node.
    """
    if every:
        _status_of_the_fleet(url, token, as_json=as_json)
        return
    if on:
        said = _asking(on, token, lambda client: client.firewall())
        if as_json:
            _dump(said.model_dump(mode="json"))
            return
        console.print(
            f"{said.backend}: " + ("present" if said.available else "not on that machine"),
            style=theme.BONE_DIM,
        )
        console.print(
            f"{said.rules} rules, {said.from_registry} from the registry",
            style=theme.BONE_DIM,
        )
        if said.rollback_at:
            console.print(f"rolling back at {said.rollback_at:%H:%M:%S}", style=theme.SHRIEKER)
        if not said.remote:
            console.print(
                "changing them there is switched off - allow_remote_firewall",
                style=theme.BONE_DIM,
            )
        return

    try:
        backend = _backend()
    except WardenError as exc:
        raise _fail(exc) from exc
    waiting = guard.armed(_snapshots())
    if as_json:
        _dump(
            {
                "backend": backend.kind,
                "available": backend.available(),
                "rules": len(_rules().list()),
                "pending": guard.pending(_rules().list(), _snapshots()).count,
                "rollback_at": waiting.deadline.isoformat() if waiting else None,
            }
        )
        return
    console.print(
        f"{backend.kind}: " + ("present" if backend.available() else "not on this machine"),
        style=theme.BONE_DIM,
    )
    drifted = guard.pending(_rules().list(), _snapshots())
    if drifted:
        console.print(
            f"{theme.plural(drifted.count, 'rule')} changed since the last apply"
            " - `warden firewall pending` says which",
            style=theme.SHRIEKER,
        )
    if waiting is not None:
        _waiting_line(waiting)


@firewall_app.command("_watch", hidden=True)
def firewall_watch(
    database: Annotated[str, typer.Option()],
    until: Annotated[str, typer.Option()],
) -> None:
    """Sit out a rollback window. Started detached; never run by hand."""
    guard.watch(database, datetime.fromisoformat(until))

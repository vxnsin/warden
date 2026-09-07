"""Rules for what may cross."""

from __future__ import annotations

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


def _what(rule: firewall.Rule) -> str:
    if rule.protocol in (firewall.Protocol.ICMP, firewall.Protocol.ANY):
        return str(rule.protocol)
    known = catalogue.named(rule.protocol, rule.ports)
    ports = firewall.spelled(rule.ports) or "any"
    return f"{rule.protocol}/{ports}" + (f" ({known})" if known else "")


def _rules_table(rules: list[firewall.Rule]) -> Table:
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("NAME", "DIR", "ACTION", "WHAT", "FROM", "ORIGIN", "UNTIL"):
        table.add_column(column)
    for rule in rules:
        table.add_row(
            Text(rule.name, style="" if rule.enabled else theme.BONE_DIM),
            str(rule.direction),
            Text(str(rule.action), style=ACTION_STYLES[rule.action]),
            _what(rule),
            rule.source,
            Text(str(rule.origin), style=RULE_ORIGINS[rule.origin]),
            Text(theme.until(rule.expires_at), style=theme.SHRIEKER)
            if rule.expires_at
            else Text("-", style=theme.BONE_DIM),
        )
    return table


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


def _asking(url: str, token: str | None, ask):
    """Put a question to another warden, and fail the way every command does."""
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
    on: Annotated[
        str | None, typer.Option("--on", help="Ask the warden at this address instead.")
    ] = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Every rule this machine holds, and where each one came from.

    `--on http://host:7010` asks that warden about its own rules, which needs
    nothing switched on there - reading is what a token already allows.
    """
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


def _rule_from(
    what: str,
    *,
    action: firewall.Action,
    source: str,
    direction: firewall.Direction,
    protocol: str | None,
    comment: str | None,
) -> firewall.Rule:
    """A port, a port range, or a name out of the catalogue."""
    if what.isdigit():
        ports = {int(what)}
        kind = firewall.Protocol(protocol or "tcp")
        origin = firewall.Origin.MANUAL
        name = f"{action}-{what}"
    else:
        kind, ports = catalogue.look_up(what)
        if protocol:
            kind = firewall.Protocol(protocol)
        origin = firewall.Origin.CATALOGUE
        name = f"{action}-{what.lower()}"
    return firewall.Rule(
        name=name,
        direction=direction,
        action=action,
        protocol=kind,
        ports=ports,
        source=source,
        origin=origin,
        comment=comment,
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

    `--on http://host:7010` asks that warden to open it on its own machine,
    which it will only do if allow_remote_firewall is set there.
    """
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
) -> None:
    """Take a rule away."""
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


@firewall_app.command("apply")
def firewall_apply(
    rollback: Annotated[
        int | None,
        typer.Option(help="Seconds to wait for a confirmation. 0 turns it off."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Make the rules true on this machine.

    A snapshot is taken first and a rollback armed, so a rule that locks you
    out undoes itself rather than needing somebody at the keyboard.
    """
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
    if waiting is not None:
        _waiting_line(waiting)


@firewall_app.command("confirm")
def firewall_confirm() -> None:
    """Keep what was applied, and call off the rollback."""
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


@firewall_app.command("status")
def firewall_status(
    on: Annotated[
        str | None, typer.Option("--on", help="Ask the warden at this address instead.")
    ] = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Whether a rollback is waiting, and what this machine can do."""
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
                "rollback_at": waiting.deadline.isoformat() if waiting else None,
            }
        )
        return
    console.print(
        f"{backend.kind}: " + ("present" if backend.available() else "not on this machine"),
        style=theme.BONE_DIM,
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

"""One command's worth of answers to "why is this not working".

Every check here is something a person would otherwise work out by running four
other commands and comparing what they said.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from warden import __version__, theme
from warden.client import WardenClient
from warden.core.config import Settings, config_file, insecure
from warden.errors import NotPermittedError, WardenError
from warden.models import Health
from warden.ports.listeners import GONE

OK = "ok"
NOTE = "note"
WARN = "warn"
FAIL = "fail"

LEVEL_COLOURS = {
    OK: theme.MOSS,
    NOTE: theme.BONE_DIM,
    WARN: theme.SHRIEKER,
    FAIL: theme.EMBER,
}

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _many(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


@dataclass(frozen=True)
class Check:
    level: str
    text: str


def exit_code(checks: list[Check]) -> int:
    """Failures are worth a non-zero exit; warnings are not.

    So this drops into a health check without a machine deciding that an
    unset token means the service is down.
    """
    return 1 if any(check.level == FAIL for check in checks) else 0


def _answering(client: WardenClient) -> tuple[list[Check], Health | None]:
    try:
        health = client.health()
    except WardenError as exc:
        return [Check(FAIL, exc.message)], None

    checks = [
        Check(OK, f"warden {health.version} answering at {client.url}, role {health.role}")
    ]
    if health.version != __version__:
        # Two versions in one place is the quiet cause of "that flag does nothing".
        checks.append(
            Check(NOTE, f"this command is {__version__}, the warden it asked is {health.version}")
        )
    return checks, health


def _settings(settings: Settings) -> list[Check]:
    path = config_file()
    checks = [
        Check(OK, f"settings from {path}")
        if path.is_file()
        else Check(OK, "settings from the environment and defaults - `warden setup` writes a file")
    ]

    exposed = settings.host not in LOOPBACK
    if exposed and not settings.token:
        checks.append(
            Check(
                WARN,
                f"listening on {settings.host} with no token set - anyone who can reach "
                "this machine can hand out and release ports",
            )
        )
    elif exposed:
        checks.append(Check(OK, f"listening on {settings.host}, token required"))

    if settings.token and settings.upstream and insecure(settings.upstream):
        checks.append(Check(WARN, f"reports to {settings.upstream} over plain http"))
    return checks


def _upstream(settings: Settings) -> list[Check]:
    """Whether the hub this warden reports to is actually there.

    Asked from here rather than read from the hub, because the thing that goes
    wrong is this machine not reaching that one.
    """
    if not settings.upstream:
        return []
    headers = (
        {"Authorization": f"Bearer {settings.cluster_token}"} if settings.cluster_token else {}
    )
    try:
        response = httpx.get(f"{settings.upstream}/health", timeout=5.0, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return [Check(FAIL, f"cannot reach the warden at {settings.upstream} - {exc}")]
    return [Check(OK, f"reporting to {settings.upstream} as {settings.node}")]


def _pool(client: WardenClient) -> list[Check]:
    try:
        pool = client.pool()
    except WardenError as exc:
        return [Check(FAIL, f"cannot read the pool - {exc.message}")]

    summary = f"pool {pool.start}-{pool.end}, {pool.allocated} held, {pool.available} free"
    if pool.available == 0:
        return [Check(FAIL, f"{summary} - the next service to ask will be turned away")]
    if pool.available <= max(1, pool.size // 10):
        return [Check(WARN, f"{summary} - nearly out")]
    return [Check(OK, summary)]


def _holders(client: WardenClient) -> list[Check]:
    try:
        services = client.services(holders=True)
    except NotPermittedError:
        # macOS will not enumerate sockets without root. Not knowing whether a
        # holder is still there is worth saying out loud; it is not this
        # machine being unwell, and it must not fail a health check.
        return _without_holders(client)
    except WardenError as exc:
        return [Check(FAIL, f"cannot list services - {exc.message}")]

    if not services:
        return [Check(OK, "nothing registered")]
    gone = [service for service in services if service.holder == GONE]
    if gone:
        return [
            Check(
                WARN,
                f"{len(gone)} of {_many(len(services), 'registration')} held by something "
                "that is gone - `warden reap`",
            )
        ]
    return [Check(OK, f"{_many(len(services), 'registration')}, every holder still there")]


def _without_holders(client: WardenClient) -> list[Check]:
    try:
        services = client.services()
    except WardenError as exc:
        return [Check(FAIL, f"cannot list services - {exc.message}")]
    return [
        Check(
            NOTE,
            f"{_many(len(services), 'registration')}, holders not checked - this "
            "system will not list sockets without root",
        )
    ]


def _nodes(client: WardenClient, health: Health) -> list[Check]:
    if not health.nodes:
        return []
    try:
        nodes = client.nodes()
    except WardenError as exc:
        return [Check(FAIL, f"cannot list nodes - {exc.message}")]
    return [
        Check(
            OK if node.status == "online" else WARN,
            f"{node.name} {node.status}, last seen {theme.age(node.last_seen)}",
        )
        for node in nodes
    ]


def _webhook(client: WardenClient) -> list[Check]:
    """Whether events are going anywhere, and whether they arrive.

    A webhook that has been failing all day looks, from inside warden, exactly
    like a week in which nothing happened. That is the whole reason for this.
    """
    try:
        status = client.webhook()
    except WardenError:
        return []  # an older warden, which has nowhere to post anything
    if not status.configured:
        return []

    where = f"events to {status.target} as {status.format}"
    if status.last_error:
        checks = [Check(WARN, f"{where} are not arriving - {status.last_error}")]
    else:
        checks = [Check(OK, f"{where}, {status.delivered} delivered")]
        if status.failed:
            checks.append(Check(NOTE, f"{status.failed} earlier ones never arrived"))
    if status.dropped:
        checks.append(Check(WARN, f"{status.dropped} events dropped - a reader fell behind"))
    return checks


def _updates(client: WardenClient) -> list[Check]:
    try:
        status = client.update_status()
    except WardenError as exc:
        return [Check(NOTE, f"could not check for updates - {exc.message}")]
    if status.reason:
        return [Check(NOTE, f"could not check for updates - {status.reason}")]
    if status.available and status.latest:
        return [Check(WARN, f"warden {status.latest} is out, this is {status.current}")]
    return [Check(OK, f"warden {status.current} is the newest there is")]


def examine(client: WardenClient, settings: Settings) -> list[Check]:
    """Everything worth knowing about this warden, in the order it matters."""
    checks, health = _answering(client)
    checks.extend(_settings(settings))
    if health is None:
        return checks
    checks.extend(_upstream(settings))
    checks.extend(_pool(client))
    checks.extend(_holders(client))
    checks.extend(_webhook(client))
    checks.extend(_nodes(client, health))
    checks.extend(_updates(client))
    return checks

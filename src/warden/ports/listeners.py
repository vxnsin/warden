"""What is actually listening on this machine, and how to make it stop.

The registry only knows what was handed out through it. This is the other half:
every socket the operating system reports, whether warden gave it out or not.
"""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime

import psutil

from warden.errors import (
    NotPermittedError,
    ProtectedProcessError,
    StillRunningError,
    UnknownProcessError,
)
from warden.models import Listener

# Stopping any of these takes the machine down with it.
SYSTEM_PIDS = frozenset({0, 1, 2, 3, 4})

PROCESS_FIELDS = ["name", "username", "create_time", "cmdline"]


def _protocol(family: int, kind: int) -> str:
    base = "tcp" if kind == socket.SOCK_STREAM else "udp"
    return f"{base}6" if family == socket.AF_INET6 else base


def _details(pid: int | None, cache: dict[int, dict]) -> dict:
    """What the operating system will tell us about a process, if anything.

    A socket owned by another user shows up without its process, which is normal
    without elevation and must not sink the whole listing.
    """
    if pid is None:
        return {}
    if pid not in cache:
        try:
            cache[pid] = psutil.Process(pid).as_dict(PROCESS_FIELDS, ad_value=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cache[pid] = {}
    return cache[pid]


def listeners(*, udp: bool = True) -> list[Listener]:
    """Every socket bound on this machine, lowest port first."""
    try:
        connections = psutil.net_connections(kind="inet")
    except psutil.AccessDenied as exc:
        raise NotPermittedError(
            "this system will not list sockets for your user - "
            "run warden as administrator to see them"
        ) from exc

    cache: dict[int, dict] = {}
    rows: list[Listener] = []
    for connection in connections:
        if not connection.laddr:
            continue
        if connection.type == socket.SOCK_STREAM:
            if connection.status != psutil.CONN_LISTEN:
                continue
        elif not udp:
            continue

        details = _details(connection.pid, cache)
        command = details.get("cmdline")
        started = details.get("create_time")
        rows.append(
            Listener(
                protocol=_protocol(connection.family, connection.type),
                host=connection.laddr.ip,
                port=connection.laddr.port,
                pid=connection.pid,
                process=details.get("name"),
                user=details.get("username"),
                started_at=datetime.fromtimestamp(started, UTC) if started else None,
                command=" ".join(command) if command else None,
            )
        )
    return sorted(rows, key=lambda row: (row.port, row.protocol, row.host))


def holder_of(port: int, *, udp: bool = True) -> Listener | None:
    """The socket sitting on a port, preferring one we can name a process for."""
    matches = [row for row in listeners(udp=udp) if row.port == port]
    if not matches:
        return None
    return next((row for row in matches if row.pid is not None), matches[0])


RUNNING = "running"
GONE = "gone"

# A service that registered a moment ago may not have bound its socket yet.
# Calling that dead would be worse than saying nothing.
SETTLING = 30


def bound_ports() -> set[int]:
    """Every port something is currently listening on, in one sweep."""
    return {row.port for row in listeners()}


def holding(
    port: int, pid: int | None, since: float, bound: set[int]
) -> tuple[str, str | None]:
    """Whether whoever asked for this port still appears to be there, and why not.

    Two signals, either of which is enough: the process it named is gone, or
    nothing is listening on the port at all.
    """
    if since < SETTLING:
        return RUNNING, None
    dead = pid is not None and not psutil.pid_exists(pid)
    silent = port not in bound
    if dead and silent:
        return GONE, f"nothing is on {port} and pid {pid} is gone"
    if dead:
        return GONE, f"pid {pid} is gone and {port} is held by something else"
    if silent:
        return GONE, f"nothing is listening on {port}"
    return RUNNING, None


def stop(pid: int, *, force: bool = False, timeout: float = 5.0) -> str:
    """End a process and return its name."""
    if pid in SYSTEM_PIDS:
        raise ProtectedProcessError(f"process {pid} belongs to the operating system")
    if pid == os.getpid():
        raise ProtectedProcessError("that process is warden itself")

    try:
        process = psutil.Process(pid)
        name = process.name()
    except psutil.NoSuchProcess as exc:
        raise UnknownProcessError(f"no process is running with id {pid}") from exc
    except psutil.AccessDenied as exc:
        raise NotPermittedError(f"not allowed to look at process {pid}") from exc

    try:
        process.terminate()
        process.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied as exc:
        raise NotPermittedError(
            f"not allowed to stop {name} ({pid}) - it belongs to another user"
        ) from exc
    except psutil.TimeoutExpired as exc:
        if not force:
            raise StillRunningError(
                f"{name} ({pid}) ignored the request to stop - pass --force to kill it"
            ) from exc
        process.kill()
        process.wait(timeout=timeout)
    return name

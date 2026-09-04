"""Running someone else's process on a port warden picked for it."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from warden.client import WardenClient
from warden.core.config import Settings, slugify
from warden.errors import PoolExhaustedError, WardenError
from warden.models import Registration
from warden.ports.allocator import PortPool, is_bound


def default_name() -> str:
    return slugify(Path.cwd().name)


def free_port(host: str = "127.0.0.1") -> int:
    """A port nothing is on, for `--anyway` when no warden is running."""
    settings = Settings()
    pool = PortPool(settings.pool_start, settings.pool_end, settings.reserved)
    for candidate in pool.candidates(taken=set()):
        if not is_bound(host, candidate):
            return candidate
    raise PoolExhaustedError(f"nothing free in {pool.start}-{pool.end} on {host}")


def environment(registration: Registration) -> dict[str, str]:
    """What the child is told, on top of what it already had.

    PORT because every framework already reads it, and the rest so a process
    that wants to know more than the number does not have to guess.
    """
    return {
        **os.environ,
        "PORT": str(registration.port),
        "WARDEN_PORT": str(registration.port),
        "WARDEN_HOST": registration.host,
        "WARDEN_ADDRESS": registration.address,
        "WARDEN_SERVICE": registration.name,
    }


def as_env(registration: Registration) -> dict[str, str]:
    """Only what warden itself has to say, for printing or writing to a file."""
    return {
        "PORT": str(registration.port),
        "WARDEN_PORT": str(registration.port),
        "WARDEN_HOST": registration.host,
        "WARDEN_ADDRESS": registration.address,
        "WARDEN_SERVICE": registration.name,
    }


def merge_dotenv(text: str, values: dict[str, str]) -> str:
    """The file with these keys set, and every other line exactly as it was.

    A .env is usually somebody else's too, so lines warden did not write are
    left alone rather than rewritten into a tidier file nobody asked for.
    """
    lines = text.splitlines()
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines).strip("\n") + "\n"


class Heartbeat:
    """Renews a lease three times over while the process it belongs to runs."""

    def __init__(self, client: WardenClient, name: str, ttl: int) -> None:
        self.client = client
        self.name = name
        self.ttl = ttl
        self.interval = max(1.0, ttl / 3)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with suppress(WardenError):
                self.client.heartbeat(self.name, ttl=self.ttl)


def supervise(
    command: Sequence[str],
    registration: Registration,
    started: Callable[[int], None] | None = None,
) -> int:
    """Run the command with the port in its environment, and hand back its code.

    `started` is handed the child's pid, which is only knowable once it exists
    and is what later tells a held port from an abandoned one.
    """
    child = subprocess.Popen(list(command), env=environment(registration))
    if started:
        started(child.pid)
    try:
        return child.wait()
    except KeyboardInterrupt:
        # The console delivered the interrupt to the child as well, so it is
        # already shutting down. Waiting lets it do that the way it would have
        # without a wrapper in front of it.
        try:
            return child.wait(timeout=10)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            child.terminate()
            return child.wait()

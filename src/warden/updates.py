"""Whether a newer warden exists, and asking a machine to go and get it."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
from contextlib import suppress
from datetime import UTC, datetime

import httpx
from packaging.version import InvalidVersion, Version

from warden import __version__
from warden.config import Settings
from warden.errors import NotPermittedError, UpdateFailedError
from warden.models import UpdateStatus

logger = logging.getLogger("warden.updates")

RELEASES = "https://api.github.com/repos/{repo}/releases/latest"

# An update is never urgent enough to justify holding a command up.
TIMEOUT = 5.0

# Long enough that a machine which cannot reach the internet is not retrying all
# day, short enough that a release is noticed the same afternoon.
INTERVAL = 6 * 60 * 60


def newer(latest: str, current: str = __version__) -> bool:
    """Whether ``latest`` is a version worth moving to."""
    try:
        return Version(latest.lstrip("vV")) > Version(current)
    except InvalidVersion:
        return False


async def check(http: httpx.AsyncClient, repo: str) -> UpdateStatus:
    """Ask GitHub for the newest release. Never raises; says why instead."""
    now = datetime.now(UTC)
    try:
        response = await http.get(RELEASES.format(repo=repo))
        if response.status_code == httpx.codes.NOT_FOUND:
            return UpdateStatus(
                current=__version__,
                checked_at=now,
                reason=f"{repo} has published no releases yet",
            )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        return UpdateStatus(current=__version__, checked_at=now, reason=str(exc))

    tag = str(body.get("tag_name") or "").lstrip("vV")
    if not tag:
        return UpdateStatus(
            current=__version__, checked_at=now, reason="the latest release has no tag"
        )
    return UpdateStatus(
        current=__version__,
        latest=tag,
        available=newer(tag),
        url=body.get("html_url"),
        checked_at=now,
    )


def _arguments(command: str) -> str | list[str]:
    return command if os.name == "nt" else shlex.split(command)


def apply(settings: Settings, timeout: float = 300.0) -> str:
    """Run whatever this machine says updating means, and report what happened.

    The command comes from this machine's own configuration and never from the
    request. A hub asks a node to update itself; it cannot tell it what to run.
    That is the whole difference between a fleet updater and a way to execute
    anything anywhere.
    """
    if not settings.allow_remote_update:
        raise NotPermittedError(
            "updating over the API is switched off - "
            "set WARDEN_ALLOW_REMOTE_UPDATE=true on this warden to allow it"
        )
    if not settings.update_command:
        raise NotPermittedError(
            "this warden has no WARDEN_UPDATE_COMMAND, so it does not know how to update itself"
        )

    logger.info("updating: %s", settings.update_command)
    try:
        finished = subprocess.run(
            # Windows parses a command line itself, and splitting it with POSIX
            # rules first would eat the backslashes out of every path on the
            # machine. No shell either way: the command is run as it is written.
            _arguments(settings.update_command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UpdateFailedError(f"cannot run the update command: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateFailedError(f"the update command ran longer than {timeout:g}s") from exc

    output = (finished.stdout + finished.stderr).strip()
    if finished.returncode != 0:
        raise UpdateFailedError(
            f"the update command exited {finished.returncode}: {output[-500:] or 'no output'}"
        )
    return output[-500:] or "done"


class UpdateWatcher:
    """Keeps an answer to 'is there a newer warden?' without anyone waiting for it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.status = UpdateStatus(current=__version__, reason="not checked yet")
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not self.settings.update_check or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="warden-updates")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        headers = {"Accept": "application/vnd.github+json"}
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as http:
            while True:
                self.status = await check(http, self.settings.update_repo)
                if self.status.available:
                    logger.info(
                        "warden %s is available, this is %s",
                        self.status.latest,
                        self.status.current,
                    )
                await asyncio.sleep(self.settings.update_interval)

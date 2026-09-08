"""`warden doctor` on a timer, saying only what changed since the last look."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from warden.core.health import FAIL, OK, WARN, Check, examine
from warden.models import HEALTH

if TYPE_CHECKING:
    from warden.core.config import Settings
    from warden.core.health import Reads
    from warden.core.store import Store

logger = logging.getLogger("warden.health")

# What counts as something being wrong. A `note` is worth knowing when somebody
# asks and is not worth a message in a chat window at three in the morning.
WRONG = (WARN, FAIL)

WORSE = {OK: 0, WARN: 1, FAIL: 2}


class Rounds:
    """Checks this machine every so often and announces what changed.

    On change only. A channel told every ten minutes that three rules are still
    unapplied is a channel people mute within the week, which is the same
    reason `port.renewed` is left out of the default subscription.

    The first look is quiet: it is what the machine was already like, not news.
    """

    def __init__(self, settings: Settings, store: Store, client: Reads) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self._wrong: dict[str, tuple[str, str]] = {}
        self._begun = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not self.settings.health_watch or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="warden-health")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.settings.health_interval)
            with suppress(Exception):
                # Never the reason a warden stops handing out ports.
                await asyncio.to_thread(self.once)

    def once(self) -> list[tuple[str, str]]:
        """Look, announce what changed, and say what was announced."""
        # Without the socket sweep: walking every socket every ten minutes to
        # say the same thing is a cost `/metrics` already refuses to pay.
        return self.notice(examine(self.client, self.settings, sweeping=False))

    def notice(self, checks: list[Check]) -> list[tuple[str, str]]:
        found = _wrong(checks)
        said: list[tuple[str, str]] = []

        for about, (level, text) in found.items():
            before = self._wrong.get(about)
            if before is None or WORSE[level] > WORSE[before[0]]:
                said.append(("worsened", about))
                self._announce("worsened", about, level, text)
        for about in self._wrong.keys() - found.keys():
            said.append(("recovered", about))
            self._announce("recovered", about, OK, _text(checks, about))

        self._wrong = found
        if not self._begun:
            # Nothing was announced for it; the state above is the baseline.
            self._begun = True
            return []
        return said

    def _announce(self, action: str, about: str, level: str, text: str) -> None:
        if not self._begun:
            return
        logger.info("health %s: %s %s", action, about, text)
        self.store.announce(HEALTH, action, about, level=level, says=text)


def _wrong(checks: list[Check]) -> dict[str, tuple[str, str]]:
    """The worst thing each check had to say, where that was anything wrong."""
    found: dict[str, tuple[str, str]] = {}
    for check in checks:
        if check.level not in WRONG:
            continue
        before = found.get(check.about)
        if before is None or WORSE[check.level] > WORSE[before[0]]:
            found[check.about] = (check.level, check.text)
    return found


def _text(checks: list[Check], about: str) -> str:
    """What that check says now that it is happy, in its own words."""
    return next(
        (check.text for check in checks if check.about == about),
        "nothing to report",
    )

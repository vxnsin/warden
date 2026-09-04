from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import httpx

from warden import __version__
from warden.core.config import Settings

logger = logging.getLogger("warden.upstream")


class UpstreamReporter:
    """Keeps this warden's entry in the hub fresh.

    Two things are not negotiable. Starting up must not wait on the hub, and a
    hub that is away must never stop this warden handing out ports. The whole
    point of the hierarchy is that a node carries on alone.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Report three times per lease, so two lost messages still leave a margin.
        self.interval = max(5.0, settings.node_ttl / 3)
        self._task: asyncio.Task[None] | None = None

    @property
    def announcement(self) -> dict[str, object]:
        return {
            "name": self.settings.node,
            "url": self.settings.advertise_url,
            "pool_start": self.settings.pool_start,
            "pool_end": self.settings.pool_end,
            "version": __version__,
        }

    def client(self) -> httpx.AsyncClient:
        headers = {}
        if self.settings.cluster_token:
            headers["Authorization"] = f"Bearer {self.settings.cluster_token}"
        return httpx.AsyncClient(
            base_url=self.settings.upstream or "", timeout=5.0, headers=headers
        )

    async def announce_once(self, http: httpx.AsyncClient) -> bool:
        """Report to the hub. Returns whether it listened."""
        try:
            response = await http.post("/v1/nodes", json=self.announcement)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("could not reach the warden at %s: %s", self.settings.upstream, exc)
            return False
        logger.info("reported to the warden at %s", self.settings.upstream)
        return True

    def start(self) -> None:
        if self.settings.upstream is None or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="warden-upstream")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        async with self.client() as http:
            while True:
                await self.announce_once(http)
                await asyncio.sleep(self.interval)

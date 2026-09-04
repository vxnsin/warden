"""What just happened, carried to whoever wants to know.

Handing out a port must never wait on somebody's chat server. The store hands
an event over the moment its change is committed, from whichever thread did the
writing; everything after that is the event loop's problem and nobody's wait.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from warden import webhooks
from warden.config import Settings
from warden.models import Event, WebhookStatus

logger = logging.getLogger("warden.events")

WATCHING = 200
OUTBOX = 500
ATTEMPTS = 3
BACKOFF = 2.0
TIMEOUT = 10.0


def redacted(url: str | None) -> str | None:
    """A webhook address is a credential, and the path is the secret half."""
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" + ("/..." if parts.path.strip("/") else "")


class EventBus:
    """One event in, everyone who is listening out."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watching: set[asyncio.Queue[Event]] = set()
        self._outbox: asyncio.Queue[Event] | None = None
        self._sender: asyncio.Task[None] | None = None
        self._delivered = 0
        self._failed = 0
        self._dropped = 0
        self._last_error: str | None = None
        self._last_sent: datetime | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self.settings.webhook:
            return
        self._outbox = asyncio.Queue(maxsize=OUTBOX)
        self._sender = asyncio.create_task(self._post_forever(), name="warden-webhook")

    async def stop(self) -> None:
        self._loop = None
        if self._sender is None:
            return
        self._sender.cancel()
        with suppress(asyncio.CancelledError):
            await self._sender
        self._sender = None

    def publish(self, event: Event) -> None:
        """Called from the thread that made the change, and it holds nothing up."""
        loop = self._loop
        if loop is None:
            return
        # A worker thread can still be mid-write while the loop is closing.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(self._fan_out, event)

    @asynccontextmanager
    async def watch(self) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=WATCHING)
        self._watching.add(queue)
        try:
            yield queue
        finally:
            self._watching.discard(queue)

    @property
    def status(self) -> WebhookStatus:
        configured = bool(self.settings.webhook)
        return WebhookStatus(
            configured=configured,
            target=redacted(self.settings.webhook),
            format=self.settings.webhook_format if configured else None,
            actions=sorted(self.settings.webhook_events) if configured else [],
            watching=len(self._watching),
            delivered=self._delivered,
            failed=self._failed,
            dropped=self._dropped,
            last_error=self._last_error,
            last_sent=self._last_sent,
        )

    def _fan_out(self, event: Event) -> None:
        for queue in self._watching:
            self._offer(queue, event)
        if self._outbox is not None and event.action in self.settings.webhook_events:
            self._offer(self._outbox, event)

    def _offer(self, queue: asyncio.Queue[Event], event: Event) -> None:
        """A backed-up reader loses the oldest event, never the newest, never the server."""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    async def _post_forever(self) -> None:
        outbox = self._outbox
        assert outbox is not None
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            while True:
                await self._post(http, await outbox.get())

    async def _post(self, http: httpx.AsyncClient, event: Event) -> None:
        body, headers = webhooks.render(
            event,
            node=self.settings.node,
            shape=self.settings.webhook_format,
            secret=self.settings.webhook_secret,
        )
        for attempt in range(ATTEMPTS):
            try:
                response = await http.post(
                    self.settings.webhook or "", content=body, headers=headers
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                if attempt + 1 == ATTEMPTS:
                    self._failed += 1
                    self._last_error = str(exc)
                    logger.warning(
                        "gave up posting %s for %s after %s tries: %s",
                        event.action,
                        event.name,
                        ATTEMPTS,
                        exc,
                    )
                    return
                await asyncio.sleep(BACKOFF**attempt)
            else:
                self._delivered += 1
                self._last_error = None
                self._last_sent = datetime.now(UTC)
                return

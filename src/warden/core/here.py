"""This warden asking itself the questions `warden doctor` usually asks over HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING

from warden import __version__
from warden.models import Health, Node, PoolStatus, Registration, UpdateStatus, WebhookStatus

if TYPE_CHECKING:
    from warden.core.config import Settings
    from warden.core.events import EventBus
    from warden.fleet.nodes import Fleet
    from warden.ports.service import Registry


class Here:
    """A warden read from inside itself, in the shape the checks expect.

    `warden doctor` reads a machine through its API, which is right when a
    person runs it and wrong when the API answers it - a server making an HTTP
    request of itself to say how it is. Every answer below is what the handler
    for that route returns, called directly.
    """

    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        fleet: Fleet,
        bus: EventBus,
        updates: object,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._fleet = fleet
        self._bus = bus
        self._updates = updates

    @property
    def url(self) -> str:
        return self._settings.advertise or f"http://{self._settings.host}:{self._settings.port}"

    def health(self) -> Health:
        return Health(
            status="ok",
            version=__version__,
            node=self._settings.node,
            role=self._settings.role,
            services=self._registry.store.count(),
            nodes=self._fleet.count(),
        )

    def pool(self) -> PoolStatus:
        return self._registry.pool_status()

    def services(
        self,
        *,
        project: str | None = None,
        kind: str | None = None,
        holders: bool = False,
    ) -> list[Registration]:
        found = self._registry.list(project=project, kind=kind)
        return self._registry.with_holders(found) if holders else found

    def nodes(self) -> list[Node]:
        return self._fleet.nodes()

    def webhook(self) -> WebhookStatus:
        return self._bus.status

    def update_status(self) -> UpdateStatus:
        return self._updates.status

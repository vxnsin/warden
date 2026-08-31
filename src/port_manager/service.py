from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from port_manager.allocator import PortPool, is_bound
from port_manager.errors import PoolExhaustedError, PortUnavailableError, UnknownServiceError
from port_manager.models import (
    HeartbeatRequest,
    PoolStatus,
    Registration,
    RegistrationRequest,
)
from port_manager.store import Store


def utcnow() -> datetime:
    return datetime.now(UTC)


class PortManager:
    """Hands out ports and keeps track of who holds them."""

    def __init__(self, store: Store, pool: PortPool, *, probe: bool = True) -> None:
        self.store = store
        self.pool = pool
        self.probe = probe

    def list(self, *, project: str | None = None, kind: str | None = None) -> list[Registration]:
        self.store.purge_expired(utcnow())
        return self.store.list(project=project, kind=kind)

    def get(self, name: str) -> Registration:
        self.store.purge_expired(utcnow())
        registration = self.store.get(name)
        if registration is None:
            raise UnknownServiceError(f"no service registered as {name!r}")
        return registration

    def register(self, request: RegistrationRequest) -> tuple[Registration, bool]:
        """Assign a port to a service. Returns the registration and whether it is new."""
        now = utcnow()
        self.store.purge_expired(now)

        existing = self.store.get(request.name)
        port = self._select_port(request, existing)

        registration = Registration(
            name=request.name,
            kind=request.kind,
            project=request.project,
            host=request.host,
            port=port,
            pid=request.pid,
            meta=request.meta,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            expires_at=now + timedelta(seconds=request.ttl) if request.ttl else None,
        )
        try:
            self.store.save(registration)
        except sqlite3.IntegrityError as exc:
            raise PortUnavailableError(f"port {port} was claimed concurrently") from exc
        return registration, existing is None

    def heartbeat(self, name: str, request: HeartbeatRequest) -> Registration:
        now = utcnow()
        registration = self.get(name)
        updated = registration.model_copy(
            update={
                "pid": request.pid if request.pid is not None else registration.pid,
                "updated_at": now,
                "expires_at": now + timedelta(seconds=request.ttl) if request.ttl else None,
            }
        )
        self.store.save(updated)
        return updated

    def release(self, name: str) -> None:
        if not self.store.delete(name):
            raise UnknownServiceError(f"no service registered as {name!r}")

    def pool_status(self) -> PoolStatus:
        self.store.purge_expired(utcnow())
        allocated = sum(1 for reg in self.store.list() if reg.port in self.pool)
        usable = self.pool.size - len(self.pool.reserved)
        return PoolStatus(
            start=self.pool.start,
            end=self.pool.end,
            size=self.pool.size,
            reserved=sorted(self.pool.reserved),
            allocated=allocated,
            available=usable - allocated,
        )

    def _select_port(
        self, request: RegistrationRequest, existing: Registration | None
    ) -> int:
        host = request.host
        taken = self.store.ports_on(host)
        holds = existing is not None and existing.host == host
        if holds:
            taken.discard(existing.port)

        wanted = request.preferred_port
        if wanted is not None:
            if holds and existing.port == wanted:
                return wanted
            if wanted in self.pool.reserved:
                raise PortUnavailableError(f"port {wanted} is reserved")
            if wanted in taken:
                owner = self.store.owner_of(host, wanted)
                raise PortUnavailableError(f"port {wanted} is held by {owner!r}")
            if self.probe and is_bound(host, wanted):
                raise PortUnavailableError(f"port {wanted} is already in use on {host}")
            return wanted

        # Keep a service on the port it had, so restarts do not move it around.
        if holds and existing.port not in taken and existing.port not in self.pool.reserved:
            return existing.port

        for candidate in self.pool.candidates(taken):
            if not self.probe or not is_bound(host, candidate):
                return candidate

        raise PoolExhaustedError(
            f"no free port left in {self.pool.start}-{self.pool.end} on {host}"
        )

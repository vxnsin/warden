from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from warden.core.store import Store
from warden.errors import PoolExhaustedError, PortUnavailableError, UnknownServiceError
from warden.models import (
    Event,
    GroupRequest,
    HeartbeatRequest,
    PoolStatus,
    Registration,
    RegistrationRequest,
)
from warden.ports.allocator import PortPool, is_bound
from warden.ports.listeners import bound_ports, holding


def utcnow() -> datetime:
    return datetime.now(UTC)


class Registry:
    """Hands out ports and keeps track of who holds them."""

    def __init__(self, store: Store, pool: PortPool, *, probe: bool = True) -> None:
        self.store = store
        self.pool = pool
        self.probe = probe

    def list(self, *, project: str | None = None, kind: str | None = None) -> list[Registration]:
        self.store.purge_expired(utcnow())
        return self.store.list(project=project, kind=kind)

    def with_holders(self, registrations: list[Registration]) -> list[Registration]:
        """The same registrations, each saying whether whoever asked is still there."""
        now = utcnow()
        bound = bound_ports()
        filled = []
        for registration in registrations:
            since = (now - registration.updated_at).total_seconds()
            holder, reason = holding(registration.port, registration.pid, since, bound)
            filled.append(
                registration.model_copy(update={"holder": holder, "holder_reason": reason})
            )
        return filled

    def history(
        self, *, port: int | None = None, name: str | None = None, limit: int = 100
    ) -> list[Event]:
        return self.store.history(port=port, name=name, limit=limit)

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
            ttl=request.ttl,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            expires_at=now + timedelta(seconds=request.ttl) if request.ttl else None,
        )
        try:
            self.store.save(registration)
        except sqlite3.IntegrityError as exc:
            raise PortUnavailableError(f"port {port} was claimed concurrently") from exc
        return registration, existing is None

    def register_group(self, request: GroupRequest) -> list[Registration]:
        """Hand out several ports at once, or none of them.

        A stack that needs four ports asks four times today, and between the
        first answer and the fourth registration anything else on the machine
        can take one of them. The caller has no way to close that gap; this
        does, by choosing and writing the whole set under one lock.
        """
        now = utcnow()
        self.store.purge_expired(now)

        names = request.members
        existing = {name: self.store.get(name) for name in names}
        ports = self._group_ports(request, existing)

        group = [
            Registration(
                name=name,
                kind=request.kind,
                project=request.project,
                host=request.host,
                port=port,
                pid=request.pid,
                meta=request.meta,
                ttl=request.ttl,
                created_at=existing[name].created_at if existing[name] else now,
                updated_at=now,
                expires_at=now + timedelta(seconds=request.ttl) if request.ttl else None,
            )
            for name, port in zip(names, ports, strict=True)
        ]
        try:
            self.store.save_many(group)
        except sqlite3.IntegrityError as exc:
            raise PortUnavailableError("a port in the group was claimed concurrently") from exc
        return group

    def _group_ports(
        self, request: GroupRequest, existing: dict[str, Registration | None]
    ) -> list[int]:
        kept = self._group_kept(request, existing)
        if kept is not None:
            return kept
        # The group's own ports are not in its way: writing a member moves it
        # off the port it had, and that port is free the moment it does.
        held = {
            member.port
            for member in existing.values()
            if member is not None and member.host == request.host
        }
        return self._group_fresh(request, self.store.ports_on(request.host) - held)

    def _group_kept(
        self, request: GroupRequest, existing: dict[str, Registration | None]
    ) -> list[int] | None:
        """Where the group already is, when that is still somewhere it may be.

        Asking twice must not shuffle a running stack onto different ports.
        """
        members = [existing[name] for name in request.members]
        if any(member is None or member.host != request.host for member in members):
            return None
        ports = [member.port for member in members if member is not None]
        if any(port not in self.pool for port in ports):
            return None
        if request.contiguous and ports != list(range(ports[0], ports[0] + len(ports))):
            return None
        return ports

    def _group_fresh(self, request: GroupRequest, taken: set[int]) -> list[int]:
        chosen: list[int] = []
        for candidate in self.pool.candidates(taken):
            if self.probe and is_bound(request.host, candidate):
                continue
            if request.contiguous and chosen and candidate != chosen[-1] + 1:
                chosen = []
            chosen.append(candidate)
            if len(chosen) == request.count:
                return chosen

        where = f"{self.pool.start}-{self.pool.end} on {request.host}"
        if request.contiguous:
            raise PoolExhaustedError(f"no run of {request.count} free ports in {where}")
        raise PoolExhaustedError(
            f"only {len(chosen)} free ports in {where}, {request.count} asked for"
        )

    def heartbeat(self, name: str, request: HeartbeatRequest) -> Registration:
        now = utcnow()
        registration = self.get(name)
        # A heartbeat without a ttl renews the lease the service registered with,
        # rather than silently turning it into a permanent registration.
        ttl = request.ttl if request.ttl is not None else registration.ttl
        updated = registration.model_copy(
            update={
                "pid": request.pid if request.pid is not None else registration.pid,
                "ttl": ttl,
                "updated_at": now,
                "expires_at": now + timedelta(seconds=ttl) if ttl else None,
            }
        )
        self.store.save(updated)
        return updated

    def release(self, name: str) -> None:
        if not self.store.delete(name):
            raise UnknownServiceError(f"no service registered as {name!r}")

    def pool_status(self) -> PoolStatus:
        self.store.purge_expired(utcnow())
        held = {reg.port for reg in self.store.list() if reg.port in self.pool}
        allocated = len(held)
        usable = self.pool.size - len(self.pool.reserved)
        return PoolStatus(
            start=self.pool.start,
            end=self.pool.end,
            size=self.pool.size,
            reserved=sorted(self.pool.reserved),
            allocated=allocated,
            available=usable - allocated,
            largest_run=self.pool.largest_run(held),
        )

    def _why_unavailable(self, host: str, port: int, taken: set[int]) -> str | None:
        """The reason a port cannot be handed out, or None if it is free."""
        if port in self.pool.reserved:
            return f"port {port} is reserved"
        if port in taken:
            return f"port {port} is held by {self.store.owner_of(host, port)!r}"
        if self.probe and is_bound(host, port):
            return f"port {port} is already in use on {host}"
        return None

    def _select_port(
        self, request: RegistrationRequest, existing: Registration | None
    ) -> int:
        host = request.host
        taken = self.store.ports_on(host)
        holds = existing is not None and existing.host == host
        if holds:
            taken.discard(existing.port)

        wanted = request.require_port if request.require_port else request.preferred_port
        if wanted is not None:
            if holds and existing.port == wanted:
                return wanted
            problem = self._why_unavailable(host, wanted, taken)
            if problem is None:
                return wanted
            if request.require_port is not None:
                raise PortUnavailableError(problem)
            # Only a preference: fall back to the pool rather than refuse the service.

        # Keep a service on the port it had, so restarts do not move it around.
        if holds and existing.port not in taken and existing.port not in self.pool.reserved:
            return existing.port

        for candidate in self.pool.candidates(taken):
            if not self.probe or not is_bound(host, candidate):
                return candidate

        raise PoolExhaustedError(
            f"no free port left in {self.pool.start}-{self.pool.end} on {host}"
        )

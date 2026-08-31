from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from warden import __version__
from warden.allocator import PortPool
from warden.config import Settings
from warden.errors import NotPermittedError, WardenError
from warden.fleet import Fleet
from warden.listeners import listeners, stop
from warden.models import (
    ErrorResponse,
    HeartbeatRequest,
    Listener,
    Node,
    NodeAnnouncement,
    PoolStatus,
    Registration,
    RegistrationRequest,
)
from warden.service import Registry
from warden.store import Store
from warden.upstream import UpstreamReporter

DESCRIPTION = """
A single place that decides which local port a service runs on.

Services register under a name, say what they are, and get a port back. The
same name always gets the same port until it is released, so a restart never
lands on a port a neighbouring service has meanwhile taken.
"""


def get_manager(request: Request) -> Registry:
    return request.app.state.manager


Manager = Annotated[Registry, Depends(get_manager)]


def get_fleet(request: Request) -> Fleet:
    return request.app.state.fleet


FleetDep = Annotated[Fleet, Depends(get_fleet)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.database)
        pool = PortPool(settings.pool_start, settings.pool_end, settings.reserved)
        app.state.settings = settings
        app.state.manager = Registry(store, pool, probe=settings.probe)
        app.state.fleet = Fleet(store, ttl=settings.node_ttl)
        # Reports in the background: a hub that is down must not hold up a node
        # that is perfectly able to hand out ports on its own.
        reporter = UpstreamReporter(settings)
        reporter.start()
        try:
            yield
        finally:
            await reporter.stop()
            store.close()

    def _check(secret: str | None, authorization: str | None, what: str) -> None:
        if secret is None:
            return
        if not authorization or not secrets.compare_digest(authorization, f"Bearer {secret}"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid or missing {what}")

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        _check(settings.token, authorization, "API token")

    def cluster(authorization: Annotated[str | None, Header()] = None) -> None:
        """Wardens talking to each other carry the cluster token, not a person's."""
        _check(settings.cluster_token, authorization, "cluster token")

    app = FastAPI(
        title="Warden",
        summary="Nothing binds a port without asking. A registry that hands out local ports.",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )

    @app.exception_handler(WardenError)
    async def _handle(_request: Request, exc: WardenError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(authorize)])

    @v1.get("/pool", summary="Pool usage")
    def pool(manager: Manager) -> PoolStatus:
        return manager.pool_status()

    @v1.get("/services", summary="List registered services")
    def list_services(
        manager: Manager, project: str | None = None, kind: str | None = None
    ) -> list[Registration]:
        return manager.list(project=project, kind=kind)

    @v1.post(
        "/services",
        summary="Register a service and receive its port",
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {"description": "Registration renewed"},
            status.HTTP_409_CONFLICT: {"model": ErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
        },
    )
    def register_service(
        request: RegistrationRequest, manager: Manager, response: Response
    ) -> Registration:
        registration, created = manager.register(request)
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return registration

    @v1.get(
        "/services/{name}",
        summary="Look up a single service",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    def get_service(name: str, manager: Manager) -> Registration:
        return manager.get(name)

    @v1.post(
        "/services/{name}/heartbeat",
        summary="Extend a registration",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    def heartbeat(name: str, request: HeartbeatRequest, manager: Manager) -> Registration:
        return manager.heartbeat(name, request)

    @v1.delete(
        "/services/{name}",
        summary="Release a port",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    def release(name: str, manager: Manager) -> None:
        manager.release(name)

    @v1.get("/listeners", summary="Every socket bound on this machine")
    def list_listeners(udp: bool = True) -> list[Listener]:
        return listeners(udp=udp)

    @v1.delete(
        "/listeners/{pid}",
        summary="Stop a process",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={
            status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
            status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        },
    )
    def stop_listener(pid: int, force: bool = False) -> None:
        # Off unless asked for: a warden reachable from the network would
        # otherwise let anyone holding the token end processes on this machine.
        if not settings.allow_kill:
            raise NotPermittedError(
                "stopping processes over the API is switched off - "
                "set WARDEN_ALLOW_KILL=true on this warden to allow it"
            )
        stop(pid, force=force)

    nodes = APIRouter(prefix="/v1/nodes", tags=["fleet"])

    @nodes.post(
        "",
        summary="Announce a warden to this one",
        dependencies=[Depends(cluster)],
        status_code=status.HTTP_201_CREATED,
        responses={status.HTTP_200_OK: {"description": "Node renewed"}},
    )
    def announce(
        announcement: NodeAnnouncement, fleet: FleetDep, response: Response
    ) -> Node:
        node, created = fleet.announce(announcement)
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return node

    @nodes.get("", summary="Every warden this one knows", dependencies=[Depends(authorize)])
    def list_nodes(fleet: FleetDep) -> list[Node]:
        return fleet.nodes()

    @nodes.delete(
        "/{name}",
        summary="Forget a warden",
        dependencies=[Depends(authorize)],
        status_code=status.HTTP_204_NO_CONTENT,
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    def forget(name: str, fleet: FleetDep) -> None:
        fleet.forget(name)

    app.include_router(v1)
    app.include_router(nodes)

    @app.get("/health", summary="Liveness probe", tags=["meta"])
    def health(manager: Manager, fleet: FleetDep) -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "node": settings.node,
            "role": settings.role,
            "services": manager.store.count(),
            "nodes": fleet.count(),
        }

    return app

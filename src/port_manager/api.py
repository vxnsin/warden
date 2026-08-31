from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from port_manager import __version__
from port_manager.allocator import PortPool
from port_manager.config import Settings
from port_manager.errors import PortManagerError
from port_manager.models import (
    ErrorResponse,
    HeartbeatRequest,
    PoolStatus,
    Registration,
    RegistrationRequest,
)
from port_manager.service import PortManager
from port_manager.store import Store

DESCRIPTION = """
A single place that decides which local port a service runs on.

Services register under a name, say what they are, and get a port back. The
same name always gets the same port until it is released, so a restart never
lands on a port a neighbouring service has meanwhile taken.
"""


def get_manager(request: Request) -> PortManager:
    return request.app.state.manager


Manager = Annotated[PortManager, Depends(get_manager)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.database)
        pool = PortPool(settings.pool_start, settings.pool_end, settings.reserved)
        app.state.settings = settings
        app.state.manager = PortManager(store, pool, probe=settings.probe)
        try:
            yield
        finally:
            store.close()

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if settings.token is None:
            return
        expected = f"Bearer {settings.token}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API token")

    app = FastAPI(
        title="Port Manager",
        summary="Central port registry for local development services.",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )

    @app.exception_handler(PortManagerError)
    async def _handle(_request: Request, exc: PortManagerError) -> JSONResponse:
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

    app.include_router(v1)

    @app.get("/health", summary="Liveness probe", tags=["meta"])
    def health(manager: Manager) -> dict[str, object]:
        return {"status": "ok", "version": __version__, "services": manager.store.count()}

    return app

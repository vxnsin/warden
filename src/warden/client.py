from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from types import TracebackType
from typing import Any

import httpx

from warden.config import DEFAULT_URL
from warden.errors import (
    NotPermittedError,
    PoolExhaustedError,
    PortUnavailableError,
    UnknownServiceError,
    WardenError,
)
from warden.models import (
    Event,
    FleetListeners,
    FleetPool,
    FleetRegistration,
    FleetServices,
    FleetUpdate,
    Health,
    Listener,
    Node,
    PoolStatus,
    Registration,
    UpdateStatus,
)

_STATUS_ERRORS: dict[int, type[WardenError]] = {
    403: NotPermittedError,
    404: UnknownServiceError,
    409: PortUnavailableError,
    503: PoolExhaustedError,
}


def resolve_url(url: str | None = None) -> str:
    return (url or os.environ.get("WARDEN_URL") or DEFAULT_URL).rstrip("/")


def detail_of(response: httpx.Response) -> str:
    """The message a warden put in an error, whatever shape it arrived in."""
    try:
        body = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else str(detail or body)


class WardenClient:
    """Talks to a running warden."""

    def __init__(
        self,
        url: str | None = None,
        *,
        token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        token = token or os.environ.get("WARDEN_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(base_url=resolve_url(url), timeout=timeout, headers=headers)

    @property
    def url(self) -> str:
        return str(self._http.base_url)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WardenClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def register(
        self,
        name: str,
        *,
        kind: str,
        project: str | None = None,
        host: str = "127.0.0.1",
        preferred_port: int | None = None,
        require_port: int | None = None,
        pid: int | None = None,
        ttl: int | None = None,
        meta: dict[str, str] | None = None,
        node: str | None = None,
    ) -> Registration:
        """Claim a port. With ``node``, on that warden in the fleet instead.

        The named node still decides: only the machine itself can tell whether
        a port is free. This warden just carries the question there.
        """
        payload = {
            "name": name,
            "kind": kind,
            "project": project,
            "host": host,
            "preferred_port": preferred_port,
            "require_port": require_port,
            "pid": pid,
            "ttl": ttl,
            "meta": meta or {},
        }
        if node:
            return FleetRegistration.model_validate(
                self._request("POST", f"/v1/fleet/services/{node}", json=payload)
            )
        return Registration.model_validate(self._request("POST", "/v1/services", json=payload))

    def lookup(self, name: str) -> Registration:
        return Registration.model_validate(self._request("GET", f"/v1/services/{name}"))

    def services(
        self,
        *,
        project: str | None = None,
        kind: str | None = None,
        holders: bool = False,
    ) -> list[Registration]:
        params: dict[str, str | bool] = {
            key: value for key, value in (("project", project), ("kind", kind)) if value
        }
        if holders:
            params["holders"] = True
        payload = self._request("GET", "/v1/services", params=params)
        return [Registration.model_validate(item) for item in payload]

    def heartbeat(
        self,
        name: str,
        *,
        pid: int | None = None,
        ttl: int | None = None,
        node: str | None = None,
    ) -> Registration:
        path = (
            f"/v1/fleet/services/{node}/{name}/heartbeat"
            if node
            else f"/v1/services/{name}/heartbeat"
        )
        payload = self._request("POST", path, json={"pid": pid, "ttl": ttl})
        model = FleetRegistration if node else Registration
        return model.model_validate(payload)

    def release(self, name: str, *, node: str | None = None) -> None:
        path = f"/v1/fleet/services/{node}/{name}" if node else f"/v1/services/{name}"
        self._request("DELETE", path)

    def health(self) -> Health:
        """What the warden says about itself, without needing a token."""
        return Health.model_validate(self._request("GET", "/health"))

    def history(
        self, *, port: int | None = None, name: str | None = None, limit: int = 100
    ) -> list[Event]:
        """What happened to a port, to a service, or lately to anything."""
        params: dict[str, object] = {"limit": limit}
        if port is not None:
            params["port"] = port
        if name is not None:
            params["name"] = name
        payload = self._request("GET", "/v1/history", params=params)
        return [Event.model_validate(item) for item in payload]

    def pool(self) -> PoolStatus:
        return PoolStatus.model_validate(self._request("GET", "/v1/pool"))

    def listeners(self, *, udp: bool = True) -> list[Listener]:
        """Every socket bound on the machine the warden runs on."""
        payload = self._request("GET", "/v1/listeners", params={"udp": udp})
        return [Listener.model_validate(item) for item in payload]

    def stop(self, pid: int, *, force: bool = False, node: str | None = None) -> None:
        path = f"/v1/fleet/listeners/{node}/{pid}" if node else f"/v1/listeners/{pid}"
        self._request("DELETE", path, params={"force": force})

    def nodes(self) -> list[Node]:
        """Every warden this one knows about."""
        return [Node.model_validate(item) for item in self._request("GET", "/v1/nodes")]

    def announce(
        self, name: str, *, url: str, pool_start: int, pool_end: int, version: str
    ) -> Node:
        payload = {
            "name": name,
            "url": url,
            "pool_start": pool_start,
            "pool_end": pool_end,
            "version": version,
        }
        return Node.model_validate(self._request("POST", "/v1/nodes", json=payload))

    def forget(self, name: str) -> None:
        self._request("DELETE", f"/v1/nodes/{name}")

    def fleet_services(
        self, *, project: str | None = None, kind: str | None = None
    ) -> FleetServices:
        """Everything the whole fleet holds, and the nodes that did not answer."""
        params = {key: value for key, value in (("project", project), ("kind", kind)) if value}
        return FleetServices.model_validate(
            self._request("GET", "/v1/fleet/services", params=params)
        )

    def fleet_listeners(self, *, udp: bool = True) -> FleetListeners:
        """Every socket bound anywhere in the fleet, each saying on which machine."""
        return FleetListeners.model_validate(
            self._request("GET", "/v1/fleet/listeners", params={"udp": udp})
        )

    def fleet_pool(self) -> FleetPool:
        """Every node's pool, and what the fleet has left altogether."""
        return FleetPool.model_validate(self._request("GET", "/v1/fleet/pool"))

    def update_status(self) -> UpdateStatus:
        """Whether the warden you are talking to knows of a newer one."""
        return UpdateStatus.model_validate(self._request("GET", "/v1/update"))

    def update_self(self) -> str:
        """Ask that warden to run its own update command."""
        return str(self._request("POST", "/v1/update")["detail"])

    def update_fleet(self) -> FleetUpdate:
        """Ask every warden in the fleet to update itself."""
        return FleetUpdate.model_validate(
            self._request("POST", "/v1/fleet/update", timeout=310.0)
        )

    def fleet_lookup(self, node: str, name: str) -> FleetRegistration:
        return FleetRegistration.model_validate(
            self._request("GET", f"/v1/fleet/services/{node}/{name}")
        )

    @contextmanager
    def session(self, name: str, **kwargs: Any) -> Iterator[Registration]:
        """Hold a port for the duration of the block and release it afterwards."""
        registration = self.register(name, **kwargs)
        try:
            yield registration
        finally:
            with suppress(WardenError):
                self.release(name)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise WardenError(
                f"no warden reachable at {self.url} - start one with 'warden serve'"
            ) from exc
        if response.is_error:
            raise _STATUS_ERRORS.get(response.status_code, WardenError)(detail_of(response))
        if response.status_code == httpx.codes.NO_CONTENT:
            return None
        return response.json()


def register(name: str, *, kind: str, url: str | None = None, **kwargs: Any) -> int:
    """Register a service and return the port it should listen on."""
    with WardenClient(url) as client:
        return client.register(name, kind=kind, **kwargs).port


@contextmanager
def reserve(name: str, *, kind: str, url: str | None = None, **kwargs: Any) -> Iterator[int]:
    """Hold a port for the duration of the block and release it afterwards."""
    with (
        WardenClient(url) as client,
        client.session(name, kind=kind, **kwargs) as registration,
    ):
        yield registration.port

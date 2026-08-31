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
    FleetRegistration,
    FleetServices,
    Listener,
    Node,
    PoolStatus,
    Registration,
)

_STATUS_ERRORS: dict[int, type[WardenError]] = {
    403: NotPermittedError,
    404: UnknownServiceError,
    409: PortUnavailableError,
    503: PoolExhaustedError,
}


def resolve_url(url: str | None = None) -> str:
    return (url or os.environ.get("WARDEN_URL") or DEFAULT_URL).rstrip("/")


def _detail(response: httpx.Response) -> str:
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
    ) -> Registration:
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
        return Registration.model_validate(self._request("POST", "/v1/services", json=payload))

    def lookup(self, name: str) -> Registration:
        return Registration.model_validate(self._request("GET", f"/v1/services/{name}"))

    def services(
        self, *, project: str | None = None, kind: str | None = None
    ) -> list[Registration]:
        params = {key: value for key, value in (("project", project), ("kind", kind)) if value}
        payload = self._request("GET", "/v1/services", params=params)
        return [Registration.model_validate(item) for item in payload]

    def heartbeat(
        self, name: str, *, pid: int | None = None, ttl: int | None = None
    ) -> Registration:
        payload = self._request(
            "POST", f"/v1/services/{name}/heartbeat", json={"pid": pid, "ttl": ttl}
        )
        return Registration.model_validate(payload)

    def release(self, name: str) -> None:
        self._request("DELETE", f"/v1/services/{name}")

    def pool(self) -> PoolStatus:
        return PoolStatus.model_validate(self._request("GET", "/v1/pool"))

    def listeners(self, *, udp: bool = True) -> list[Listener]:
        """Every socket bound on the machine the warden runs on."""
        payload = self._request("GET", "/v1/listeners", params={"udp": udp})
        return [Listener.model_validate(item) for item in payload]

    def stop(self, pid: int, *, force: bool = False) -> None:
        self._request("DELETE", f"/v1/listeners/{pid}", params={"force": force})

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
            raise _STATUS_ERRORS.get(response.status_code, WardenError)(_detail(response))
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

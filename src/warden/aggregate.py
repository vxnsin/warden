"""Asking every node at once, and being honest about the ones that did not answer."""

from __future__ import annotations

import asyncio

import httpx

from warden.errors import UnknownNodeError, UnknownServiceError
from warden.models import (
    FleetRegistration,
    FleetServices,
    Node,
    Registration,
    Unreachable,
)

# Long enough for a busy machine on a local network, short enough that a rack of
# dead nodes does not make the listing feel broken. They are asked in parallel,
# so this is the wait for the whole fleet, not for each node.
TIMEOUT = 3.0


def reason(exc: Exception) -> str:
    """Why a node did not answer, in words worth showing someone."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == httpx.codes.UNAUTHORIZED:
            return "refused the token - check WARDEN_CLUSTER_TOKEN matches"
        return f"answered {code}"
    if isinstance(exc, httpx.ConnectError):
        return "could not be reached"
    if isinstance(exc, httpx.TimeoutException):
        return f"did not answer within {TIMEOUT:g}s"
    return str(exc) or exc.__class__.__name__


def client(token: str | None, timeout: float = TIMEOUT) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(timeout=timeout, headers=headers)


async def _services_of(
    http: httpx.AsyncClient, node: Node, params: dict[str, str]
) -> tuple[Node, list[Registration] | None, str | None]:
    try:
        response = await http.get(f"{node.url}/v1/services", params=params)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return node, None, reason(exc)
    return node, [Registration.model_validate(item) for item in response.json()], None


def _tag(node: str, registrations: list[Registration]) -> list[FleetRegistration]:
    return [
        FleetRegistration(node=node, **registration.model_dump())
        for registration in registrations
    ]


async def gather_services(
    http: httpx.AsyncClient,
    nodes: list[Node],
    *,
    here: str,
    local: list[Registration],
    project: str | None = None,
    kind: str | None = None,
) -> FleetServices:
    """Everything the fleet holds: this warden's own, plus every node's.

    Stale nodes are asked too. A node the hub lost sight of may be perfectly
    well and simply unable to report, and skipping it would hide real services.
    """
    params = {key: value for key, value in (("project", project), ("kind", kind)) if value}
    answers = await asyncio.gather(*(_services_of(http, node, params) for node in nodes))

    services = _tag(here, local)
    unreachable: list[Unreachable] = []
    for node, registrations, why in answers:
        if registrations is None:
            unreachable.append(Unreachable(node=node.name, url=node.url, reason=why or ""))
        else:
            services.extend(_tag(node.name, registrations))

    services.sort(key=lambda service: (service.node, service.port))
    unreachable.sort(key=lambda node: node.node)
    return FleetServices(services=services, unreachable=unreachable)


async def lookup_on(
    http: httpx.AsyncClient, nodes: list[Node], node_name: str, service: str
) -> FleetRegistration:
    """One service on one named node."""
    node = next((candidate for candidate in nodes if candidate.name == node_name), None)
    if node is None:
        raise UnknownNodeError(f"no node registered as {node_name!r}")
    try:
        response = await http.get(f"{node.url}/v1/services/{service}")
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == httpx.codes.NOT_FOUND:
            raise UnknownServiceError(
                f"no service registered as {service!r} on {node_name!r}"
            ) from exc
        raise UnknownNodeError(f"{node_name} {reason(exc)}") from exc
    except httpx.HTTPError as exc:
        raise UnknownNodeError(f"{node_name} {reason(exc)}") from exc
    return FleetRegistration(node=node_name, **response.json())

"""Asking every node at once, and being honest about the ones that did not answer."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from typing import TypeVar

import httpx

from warden.client import detail_of
from warden.errors import RelayedError, UnknownNodeError, UnknownServiceError
from warden.models import (
    Duplicate,
    FleetListener,
    FleetListeners,
    FleetPool,
    FleetRegistration,
    FleetServices,
    FleetUpdate,
    Listener,
    Node,
    NodePool,
    PoolStatus,
    Registration,
    Unreachable,
    UpdateResult,
)

Answer = TypeVar("Answer")

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


def relaying(authorization: str | None, timeout: float = TIMEOUT) -> httpx.AsyncClient:
    """A client carrying the caller's own credentials and none of its own.

    Registering changes something, and the cluster token deliberately opens
    nothing that does. So the hub forwards the authorization exactly as it
    arrived: it passes a request along, it never vouches for one.
    """
    headers = {"Authorization": authorization} if authorization else {}
    return httpx.AsyncClient(timeout=timeout, headers=headers)


def _named(nodes: list[Node], name: str) -> Node:
    node = next((candidate for candidate in nodes if candidate.name == name), None)
    if node is None:
        raise UnknownNodeError(f"no node registered as {name!r}")
    return node


def _apart(
    answers: Sequence[tuple[Node, Answer | None, str | None]],
) -> tuple[list[tuple[str, Answer]], list[Unreachable]]:
    """What came back, and the nodes it did not come back from."""
    answered: list[tuple[str, Answer]] = []
    unreachable: list[Unreachable] = []
    for node, answer, why in answers:
        if answer is None:
            unreachable.append(Unreachable(node=node.name, url=node.url, reason=why or ""))
        else:
            answered.append((node.name, answer))
    unreachable.sort(key=lambda node: node.node)
    return answered, unreachable


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


def _duplicates(services: list[FleetRegistration]) -> list[Duplicate]:
    """Names that more than one node hands out.

    A name is unique per node, never across the fleet, so this is the only place
    the clash can be seen at all. Two machines answering to `shop-api` is nearly
    always two projects that drifted apart rather than anybody's plan, and it is
    the fleet view that quietly hid it until now.
    """
    holders: dict[str, set[str]] = defaultdict(set)
    for service in services:
        holders[service.name].add(service.node)
    return [
        Duplicate(name=name, nodes=sorted(nodes))
        for name, nodes in sorted(holders.items())
        if len(nodes) > 1
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
    answered, unreachable = _apart(answers)

    services = _tag(here, local)
    for name, registrations in answered:
        services.extend(_tag(name, registrations))

    services.sort(key=lambda service: (service.node, service.port))
    # Named against the same filter the listing used: a clash the caller cannot
    # see in the table above it would only be confusing.
    return FleetServices(
        services=services, unreachable=unreachable, duplicates=_duplicates(services)
    )


async def _listeners_of(
    http: httpx.AsyncClient, node: Node, params: dict[str, object]
) -> tuple[Node, list[Listener] | None, str | None]:
    try:
        response = await http.get(f"{node.url}/v1/listeners", params=params)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return node, None, reason(exc)
    return node, [Listener.model_validate(item) for item in response.json()], None


async def gather_listeners(
    http: httpx.AsyncClient,
    nodes: list[Node],
    *,
    here: str,
    local: list[Listener],
    udp: bool = True,
) -> FleetListeners:
    """Every socket the fleet has bound, each one saying which machine it is on.

    A port number on its own means nothing across machines: 3000 on two nodes is
    two unrelated processes, and the node is what tells them apart.
    """
    answers = await asyncio.gather(
        *(_listeners_of(http, node, {"udp": udp}) for node in nodes)
    )
    answered, unreachable = _apart(answers)

    listeners = [FleetListener(node=here, **item.model_dump()) for item in local]
    for name, found in answered:
        listeners.extend(FleetListener(node=name, **item.model_dump()) for item in found)
    listeners.sort(key=lambda listener: (listener.node, listener.port, listener.protocol))
    return FleetListeners(listeners=listeners, unreachable=unreachable)


async def _pool_of(
    http: httpx.AsyncClient, node: Node
) -> tuple[Node, PoolStatus | None, str | None]:
    try:
        response = await http.get(f"{node.url}/v1/pool")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return node, None, reason(exc)
    return node, PoolStatus.model_validate(response.json()), None


async def gather_pools(
    http: httpx.AsyncClient, nodes: list[Node], *, here: str, local: PoolStatus
) -> FleetPool:
    """How much every node has left, so a machine running out is visible.

    Each node keeps its own range, and two nodes may well hand out the same
    numbers on different machines. The totals are therefore a sum of what is
    left, never one pool the fleet shares.
    """
    answers = await asyncio.gather(*(_pool_of(http, node) for node in nodes))
    answered, unreachable = _apart(answers)

    pools = [NodePool(node=here, **local.model_dump())]
    pools.extend(NodePool(node=name, **status.model_dump()) for name, status in answered)
    pools.sort(key=lambda pool: pool.node)
    return FleetPool(pools=pools, unreachable=unreachable)


async def _update_one(http: httpx.AsyncClient, node: Node) -> UpdateResult:
    try:
        response = await http.post(f"{node.url}/v1/update")
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return UpdateResult(
            node=node.name, url=node.url, ok=False, detail=detail_of(exc.response)
        )
    except httpx.HTTPError as exc:
        return UpdateResult(node=node.name, url=node.url, ok=False, detail=reason(exc))
    return UpdateResult(
        node=node.name,
        url=node.url,
        ok=True,
        detail=str(response.json().get("detail", "done")),
    )


async def update_fleet(
    http: httpx.AsyncClient, nodes: list[Node], *, here: UpdateResult
) -> FleetUpdate:
    """Ask every node to update itself.

    Each node decides what that means; the hub sends no command, only the
    request. A node with nothing configured refuses, and says so.
    """
    results = list(await asyncio.gather(*(_update_one(http, node) for node in nodes)))
    results.append(here)
    results.sort(key=lambda result: result.node)
    return FleetUpdate(results=results)


async def lookup_on(
    http: httpx.AsyncClient, nodes: list[Node], node_name: str, service: str
) -> FleetRegistration:
    """One service on one named node."""
    node = _named(nodes, node_name)
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


async def _relay(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    """Put one request to one named node, and hand back what it answered.

    The node still owns the decision - it is the machine that can try to bind
    the port - so the hub adds nothing to the question and nothing to the
    answer. What comes back refused comes back refused in the node's own words.
    """
    node = _named(nodes, node_name)
    try:
        response = await http.request(method, f"{node.url}{path}", **kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RelayedError(detail_of(exc.response), exc.response.status_code) from exc
    except httpx.HTTPError as exc:
        raise UnknownNodeError(f"{node_name} {reason(exc)}") from exc
    return response


async def register_on(
    http: httpx.AsyncClient, nodes: list[Node], node_name: str, payload: dict[str, object]
) -> tuple[FleetRegistration, bool]:
    """Ask one node for a port. Returns the registration and whether it is new."""
    response = await _relay(http, nodes, node_name, "POST", "/v1/services", json=payload)
    created = response.status_code == httpx.codes.CREATED
    return FleetRegistration(node=node_name, **response.json()), created


async def heartbeat_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    service: str,
    payload: dict[str, object],
) -> FleetRegistration:
    """Extend a lease held by one node."""
    response = await _relay(
        http, nodes, node_name, "POST", f"/v1/services/{service}/heartbeat", json=payload
    )
    return FleetRegistration(node=node_name, **response.json())


async def release_on(
    http: httpx.AsyncClient, nodes: list[Node], node_name: str, service: str
) -> None:
    """Give a port back on the node that handed it out."""
    await _relay(http, nodes, node_name, "DELETE", f"/v1/services/{service}")


async def stop_on(
    http: httpx.AsyncClient, nodes: list[Node], node_name: str, pid: int, *, force: bool = False
) -> None:
    """Ask one node to stop a process of its own.

    Its own `WARDEN_ALLOW_KILL` is still the gate, and a node with it switched
    off refuses in as many words. A pid means nothing off the machine it is on,
    which is the whole reason this goes by node rather than by number.
    """
    await _relay(
        http, nodes, node_name, "DELETE", f"/v1/listeners/{pid}", params={"force": force}
    )

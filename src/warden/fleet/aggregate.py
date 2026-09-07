"""Asking every node at once, and being honest about the ones that did not answer."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import TypeVar

import httpx

from warden.client import detail_of
from warden.core.config import insecure
from warden.errors import NotPermittedError, RelayedError, UnknownNodeError, UnknownServiceError
from warden.models import (
    Duplicate,
    FirewallResult,
    FirewallStatus,
    FleetFirewall,
    FleetFirewallResult,
    FleetListener,
    FleetListeners,
    FleetPool,
    FleetRegistration,
    FleetRules,
    FleetServices,
    FleetUpdate,
    Listener,
    Node,
    NodeFirewall,
    NodePool,
    PoolStatus,
    Registration,
    Unreachable,
    UpdateResult,
)

Answer = TypeVar("Answer")

logger = logging.getLogger("warden.fleet")

# Named once per node, not once per request.
_warned: set[str] = set()

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


def _named(nodes: list[Node], name: str, *, require_https: bool = False) -> Node:
    """The node by that name, if a token may be sent to where it says it is."""
    node = next((candidate for candidate in nodes if candidate.name == name), None)
    if node is None:
        raise UnknownNodeError(f"no node registered as {name!r}")
    if insecure(node.url):
        if require_https:
            raise NotPermittedError(
                f"{name} is at {node.url} and this warden requires HTTPS; "
                "a token sent there would cross the network in the clear"
            )
        if name not in _warned:
            _warned.add(name)
            logger.warning(
                "sending a token to %s over plain HTTP; set WARDEN_REQUIRE_HTTPS "
                "once the fleet can speak it",
                node.url,
            )
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


async def _firewall_of(
    http: httpx.AsyncClient, node: Node
) -> tuple[Node, FirewallStatus | None, str | None]:
    try:
        response = await http.get(f"{node.url}/v1/firewall")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return node, None, reason(exc)
    return node, FirewallStatus.model_validate(response.json()), None


async def gather_firewalls(
    http: httpx.AsyncClient, nodes: list[Node], *, here: str, local: FirewallStatus
) -> FleetFirewall:
    """What every node's firewall is, and which of them is about to undo itself.

    Nothing is added up. Each machine decides for itself what may cross it, so
    a total would be a number about nothing.
    """
    answers = await asyncio.gather(*(_firewall_of(http, node) for node in nodes))
    answered, unreachable = _apart(answers)

    firewalls = [NodeFirewall(node=here, **local.model_dump())]
    firewalls.extend(
        NodeFirewall(node=name, **status.model_dump()) for name, status in answered
    )
    firewalls.sort(key=lambda one: one.node)
    return FleetFirewall(firewalls=firewalls, unreachable=unreachable)


async def _rules_of(
    http: httpx.AsyncClient, node: Node, params: dict[str, str]
) -> tuple[Node, list[dict[str, object]] | None, str | None]:
    try:
        response = await http.get(f"{node.url}/v1/firewall/rules", params=params)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return node, None, reason(exc)
    return node, list(response.json()), None


async def gather_rules(
    http: httpx.AsyncClient,
    nodes: list[Node],
    *,
    here: str,
    local: list[dict[str, object]],
    origin: str | None = None,
) -> FleetRules:
    """Every rule anywhere in the fleet, each carrying the node it is on.

    A rule is passed through as the node sent it rather than parsed, so a hub
    still lists a fleet running a newer warden than itself instead of refusing
    the whole answer over one field it has not heard of.
    """
    params = {"origin": origin} if origin else {}
    answers = await asyncio.gather(*(_rules_of(http, node, params) for node in nodes))
    answered, unreachable = _apart(answers)

    rules = [{**rule, "node": here} for rule in local]
    for name, theirs in answered:
        rules.extend({**rule, "node": name} for rule in theirs)
    rules.sort(key=lambda rule: (str(rule.get("node")), str(rule.get("name"))))
    return FleetRules(rules=rules, unreachable=unreachable)


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
    require_https: bool = False,
    **kwargs: object,
) -> httpx.Response:
    """Put one request to one named node, and hand back what it answered.

    The node still owns the decision - it is the machine that can try to bind
    the port - so the hub adds nothing to the question and nothing to the
    answer. What comes back refused comes back refused in the node's own words.
    """
    node = _named(nodes, node_name, require_https=require_https)
    try:
        response = await http.request(method, f"{node.url}{path}", **kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RelayedError(detail_of(exc.response), exc.response.status_code) from exc
    except httpx.HTTPError as exc:
        raise UnknownNodeError(f"{node_name} {reason(exc)}") from exc
    return response


async def register_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    payload: dict[str, object],
    *,
    require_https: bool = False,
) -> tuple[FleetRegistration, bool]:
    """Ask one node for a port. Returns the registration and whether it is new."""
    response = await _relay(
        http, nodes, node_name, "POST", "/v1/services",
        require_https=require_https, json=payload,
    )
    created = response.status_code == httpx.codes.CREATED
    return FleetRegistration(node=node_name, **response.json()), created


async def heartbeat_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    service: str,
    payload: dict[str, object],
    *,
    require_https: bool = False,
) -> FleetRegistration:
    """Extend a lease held by one node."""
    response = await _relay(
        http, nodes, node_name, "POST", f"/v1/services/{service}/heartbeat",
        require_https=require_https, json=payload,
    )
    return FleetRegistration(node=node_name, **response.json())


async def release_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    service: str,
    *,
    require_https: bool = False,
) -> None:
    """Give a port back on the node that handed it out."""
    await _relay(
        http, nodes, node_name, "DELETE", f"/v1/services/{service}",
        require_https=require_https,
    )


async def stop_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    pid: int,
    *,
    force: bool = False,
    require_https: bool = False,
) -> None:
    """Ask one node to stop a process of its own.

    Its own `WARDEN_ALLOW_KILL` is still the gate, and a node with it switched
    off refuses in as many words. A pid means nothing off the machine it is on,
    which is the whole reason this goes by node rather than by number.
    """
    await _relay(
        http, nodes, node_name, "DELETE", f"/v1/listeners/{pid}",
        require_https=require_https, params={"force": force},
    )


async def open_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    payload: dict[str, object],
    *,
    require_https: bool = False,
) -> dict[str, object]:
    """Ask one node to open the port a service of its own holds.

    The request names a service, never a port. Which port that is, and for how
    long, is the node's own answer - and the eight bounds it has to pass are
    checked there rather than here.
    """
    response = await _relay(
        http, nodes, node_name, "POST", "/v1/firewall/open",
        require_https=require_https, json=payload,
    )
    return {**response.json(), "node": node_name}


async def write_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    payload: dict[str, object],
    *,
    require_https: bool = False,
) -> dict[str, object]:
    """Write a rule down on one node, in that node's own words."""
    response = await _relay(
        http, nodes, node_name, "POST", "/v1/firewall/rules",
        require_https=require_https, json=payload,
    )
    return {**response.json(), "node": node_name}


async def close_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    rule: str,
    *,
    require_https: bool = False,
) -> None:
    """Take one rule back out on the node that holds it."""
    await _relay(
        http, nodes, node_name, "DELETE", f"/v1/firewall/rules/{rule}",
        require_https=require_https,
    )


async def firewall_on(
    http: httpx.AsyncClient,
    nodes: list[Node],
    node_name: str,
    what: str,
    *,
    params: dict[str, object] | None = None,
    require_https: bool = False,
) -> dict[str, object]:
    """`apply`, `confirm` or `restore` on one named node."""
    response = await _relay(
        http, nodes, node_name, "POST", f"/v1/firewall/{what}",
        require_https=require_https, params=params or {},
    )
    return dict(response.json())


async def _firewall_one(
    http: httpx.AsyncClient, node: Node, what: str, params: dict[str, object]
) -> FirewallResult:
    try:
        response = await http.post(f"{node.url}/v1/firewall/{what}", params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return FirewallResult(
            node=node.name, url=node.url, ok=False, detail=detail_of(exc.response)
        )
    except httpx.HTTPError as exc:
        return FirewallResult(node=node.name, url=node.url, ok=False, detail=reason(exc))
    return FirewallResult(
        node=node.name, url=node.url, ok=True, detail=_said(what, response.json())
    )


def _said(what: str, answered: dict[str, object]) -> str:
    """What a node did, in a line that fits a column."""
    if what == "apply":
        rules = answered.get("applied", 0)
        until = answered.get("rollback_at")
        kept = f", rolling back at {str(until)[11:19]}" if until else ", no rollback armed"
        return f"{rules} rules{kept}"
    if what == "confirm":
        return f"kept snapshot {answered.get('confirmed')}"
    return f"back to snapshot {answered.get('restored')}"


async def firewall_fleet(
    http: httpx.AsyncClient,
    nodes: list[Node],
    what: str,
    *,
    params: dict[str, object] | None = None,
    here: FirewallResult,
) -> FleetFirewallResult:
    """Ask every node to do the same thing to its own firewall.

    Nobody waits for anybody. A node that refuses, or that cannot be reached at
    all, gets a line of its own and does not stop the rest - and a node that
    was never reached has already saved itself, because the rollback it armed
    runs on its own machine.
    """
    results = list(
        await asyncio.gather(
            *(_firewall_one(http, node, what, params or {}) for node in nodes)
        )
    )
    results.append(here)
    results.sort(key=lambda result: result.node)
    return FleetFirewallResult(results=results)


async def open_where_held(
    http: httpx.AsyncClient,
    nodes: list[Node],
    service: str,
    payload: dict[str, object],
    *,
    here: str,
    holders: set[str],
    require_https: bool = False,
) -> FleetFirewallResult:
    """Open a service on every node that actually holds it.

    A name means a different port on every machine, and nothing on most of
    them. A node that never registered this service is skipped and said so -
    it is not a failure, and it is not something to open anything for.
    """
    wanted = [node for node in nodes if node.name in holders]
    results = [
        FirewallResult(
            node=node.name, url=node.url, ok=False, detail=f"does not hold {service}"
        )
        for node in nodes
        if node.name not in holders
    ]

    async def one(node: Node) -> FirewallResult:
        try:
            opened = await open_on(
                http, nodes, node.name, payload, require_https=require_https
            )
        except (RelayedError, UnknownNodeError, NotPermittedError) as exc:
            return FirewallResult(
                node=node.name, url=node.url, ok=False, detail=str(exc.message)
            )
        return FirewallResult(
            node=node.name, url=node.url, ok=True, detail=_opened(opened)
        )

    results.extend(await asyncio.gather(*(one(node) for node in wanted)))
    if here not in holders:
        results.append(
            FirewallResult(node=here, url="", ok=False, detail=f"does not hold {service}")
        )
    results.sort(key=lambda result: result.node)
    return FleetFirewallResult(results=results)


def _opened(rule: dict[str, object]) -> str:
    ports = rule.get("ports") or []
    where = ", ".join(str(port) for port in ports) if isinstance(ports, list) else str(ports)
    return f"{rule.get('name')} - {where} from {rule.get('source')}"


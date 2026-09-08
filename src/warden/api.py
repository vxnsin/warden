from __future__ import annotations

import asyncio
import secrets
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from warden import __version__
from warden.core import asking, config, metrics, updates
from warden.core.config import Grant, Settings
from warden.core.events import EventBus
from warden.core.health import examine, says, worst
from warden.core.here import Here
from warden.core.rounds import Rounds
from warden.core.store import RuleStore, Snapshots, Store
from warden.errors import NotPermittedError, WardenError
from warden.firewall import catalogue, guard, link
from warden.firewall import model as firewall
from warden.firewall.backends import base
from warden.fleet import aggregate
from warden.fleet.nodes import Fleet
from warden.fleet.upstream import UpstreamReporter
from warden.models import (
    ErrorResponse,
    Event,
    FirewallResult,
    FirewallStatus,
    FleetFirewall,
    FleetFirewallResult,
    FleetListeners,
    FleetPool,
    FleetRegistration,
    FleetReport,
    FleetRules,
    FleetServices,
    FleetUpdate,
    FleetVerdict,
    GroupRequest,
    Health,
    HeartbeatRequest,
    Listener,
    Node,
    NodeAnnouncement,
    NodeVerdict,
    OpenRequest,
    PoolStatus,
    Registration,
    RegistrationRequest,
    Report,
    RuleRequest,
    Said,
    UpdateResult,
    UpdateStatus,
    Verdict,
    WebhookStatus,
)
from warden.ports.allocator import PortPool
from warden.ports.listeners import listeners, stop
from warden.ports.service import Registry

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


def get_events(request: Request) -> EventBus:
    return request.app.state.events


Events = Annotated[EventBus, Depends(get_events)]


def get_rules(request: Request) -> RuleStore:
    return request.app.state.rules


Rules = Annotated[RuleStore, Depends(get_rules)]


def get_snapshots(request: Request) -> Snapshots:
    return request.app.state.snapshots


SnapshotsDep = Annotated[Snapshots, Depends(get_snapshots)]

# Long enough that a comment down an idle stream is rare, short enough that a
# proxy in the middle does not decide the connection died.
KEEPALIVE = 20.0

# What `POST /v1/fleet/firewall/{node}/{what}` will accept, so a typo comes back
# a 404 with the list in it rather than a relayed request to a made-up path.
DOABLE = frozenset({"apply", "confirm", "restore"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.database)
        pool = PortPool(settings.pool_start, settings.pool_end, settings.reserved)
        app.state.settings = settings
        app.state.manager = Registry(store, pool, probe=settings.probe)
        app.state.rules = RuleStore(store)
        app.state.snapshots = Snapshots(store)
        app.state.fleet = Fleet(
            store, ttl=settings.node_ttl, require_https=settings.require_https
        )
        bus = EventBus(settings)
        bus.start()
        store.subscribe(bus.publish)
        app.state.events = bus
        # Reports in the background: a hub that is down must not hold up a node
        # that is perfectly able to hand out ports on its own.
        reporter = UpstreamReporter(settings)
        reporter.start()
        watcher = updates.UpdateWatcher(settings)
        watcher.start()
        app.state.updates = watcher
        # The same checks `warden doctor` runs, on a timer, announcing a
        # finding when it changes. The whole point of an event stream is not
        # having to ask, and until now warden only ever said what happened to a
        # port - never that something was wrong.
        here = Here(settings, app.state.manager, app.state.fleet, bus, watcher)
        rounds = Rounds(settings, store, here)
        rounds.start()
        app.state.rounds = rounds
        try:
            yield
        finally:
            await rounds.stop()
            await watcher.stop()
            await reporter.stop()
            await bus.stop()
            store.close()

    def _matches(secret: str | None, authorization: str | None) -> bool:
        return bool(
            secret
            and authorization
            and secrets.compare_digest(authorization, f"Bearer {secret}")
        )

    def _holder(authorization: str | None) -> Grant | None:
        """Which token this is, if it is one at all.

        Every grant is compared, never stopping at the first match, so the time
        this takes says nothing about which token was sent.
        """
        found = None
        for grant in settings.grants():
            if _matches(grant.secret, authorization):
                found = grant
        return found

    def allowed(scope: str):
        """A dependency that lets through a token reaching at least this far.

        Async on purpose. A sync dependency runs in a threadpool, and a name
        set on the context there is set on a copy that is thrown away when it
        returns - so the store would write down nobody. An async one runs in
        the request's own context, and the endpoint inherits it.
        """

        async def check(authorization: Annotated[str | None, Header()] = None) -> None:
            grants = settings.grants()
            if not grants:
                # Nothing written down is the loopback default, and has always
                # meant no check rather than no access.
                return
            grant = _holder(authorization)
            if grant is None:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "invalid or missing token"
                )
            if not grant.may(scope):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"the token {grant.name!r} may only {grant.scope}, and this is "
                    f"a {scope} thing to do",
                )
            asking.set_to(grant.name)

        return check

    async def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        """Anything a person does to the registry."""
        await allowed(config.REGISTRY)(authorization)

    async def cluster(authorization: Annotated[str | None, Header()] = None) -> None:
        """Announcing. A node must manage this without a person's token."""
        if settings.cluster_token is None:
            return
        if not _matches(settings.cluster_token, authorization):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid or missing cluster token"
            )
        asking.set_to("cluster")

    async def known_caller(authorization: Annotated[str | None, Header()] = None) -> None:
        """A person with a token that reads, or another warden with the cluster one.

        The cluster token only ever adds access; it can never open a door a
        person's token has closed.
        """
        if _matches(settings.cluster_token, authorization):
            asking.set_to("cluster")
            return
        await allowed(config.READ)(authorization)

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
    reads = APIRouter(prefix="/v1", dependencies=[Depends(known_caller)])

    @reads.get("/pool", summary="Pool usage")
    def pool(manager: Manager) -> PoolStatus:
        return manager.pool_status()

    @reads.get("/services", summary="List registered services")
    def list_services(
        manager: Manager,
        project: str | None = None,
        kind: str | None = None,
        holders: bool = False,
    ) -> list[Registration]:
        found = manager.list(project=project, kind=kind)
        return manager.with_holders(found) if holders else found

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

    @reads.get(
        "/services/{name}",
        summary="Look up a single service",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    def get_service(name: str, manager: Manager) -> Registration:
        return manager.get(name)

    @v1.post(
        "/groups",
        summary="Register several ports for one thing at once",
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_409_CONFLICT: {"model": ErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
        },
    )
    def register_group(request: GroupRequest, manager: Manager) -> list[Registration]:
        return manager.register_group(request)

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

    @reads.get("/history", summary="What happened to a port or a service")
    def history(
        manager: Manager,
        port: int | None = None,
        name: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[Event]:
        return manager.history(port=port, name=name, limit=limit)

    @reads.get(
        "/events",
        summary="What is happening, as it happens",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def stream(bus: Events) -> StreamingResponse:
        async def lines() -> AsyncIterator[str]:
            async with bus.watch() as queue:
                # Sent straight away: a client should learn it is connected
                # before anything happens, not after.
                yield ": watching\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), KEEPALIVE)
                    except TimeoutError:
                        yield ": still here\n\n"
                        continue
                    yield f"event: {event.action}\ndata: {event.model_dump_json()}\n\n"

        return StreamingResponse(
            lines(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @reads.get("/webhook", summary="Where events are posted, and whether that is working")
    def webhook(bus: Events) -> WebhookStatus:
        return bus.status

    @reads.get("/listeners", summary="Every socket bound on this machine")
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

    @nodes.get("", summary="Every warden this one knows", dependencies=[Depends(known_caller)])
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

    fleet_view = APIRouter(
        prefix="/v1/fleet", tags=["fleet"], dependencies=[Depends(known_caller)]
    )

    @fleet_view.get("/services", summary="Everything the whole fleet holds")
    async def fleet_services(
        manager: Manager,
        fleet: FleetDep,
        project: str | None = None,
        kind: str | None = None,
    ) -> FleetServices:
        async with aggregate.client(settings.cluster_token) as http:
            return await aggregate.gather_services(
                http,
                fleet.nodes(),
                here=settings.node,
                local=manager.list(project=project, kind=kind),
                project=project,
                kind=kind,
            )

    @fleet_view.get("/pool", summary="How much of its pool every node has left")
    async def fleet_pool(manager: Manager, fleet: FleetDep) -> FleetPool:
        async with aggregate.client(settings.cluster_token) as http:
            return await aggregate.gather_pools(
                http, fleet.nodes(), here=settings.node, local=manager.pool_status()
            )

    @fleet_view.get("/listeners", summary="Every socket bound anywhere in the fleet")
    async def fleet_listeners(fleet: FleetDep, udp: bool = True) -> FleetListeners:
        async with aggregate.client(settings.cluster_token) as http:
            return await aggregate.gather_listeners(
                http, fleet.nodes(), here=settings.node, local=listeners(udp=udp), udp=udp
            )

    @fleet_view.get(
        "/services/{node}/{name}",
        summary="One service on one named node",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    async def fleet_lookup(
        node: str, name: str, manager: Manager, fleet: FleetDep
    ) -> FleetRegistration:
        if node == settings.node:
            return FleetRegistration(node=node, **manager.get(name).model_dump())
        async with aggregate.client(settings.cluster_token) as http:
            return await aggregate.lookup_on(http, fleet.nodes(), node, name)

    # Reading is what a token already allows, here as much as on one machine.
    # `firewall_status` and `firewall_rules` are the same handlers the single
    # node uses, called for this warden's own answer before anyone is asked.
    @fleet_view.get("/doctor", summary="What every node has to say about itself")
    async def fleet_doctor(
        request: Request, manager: Manager, fleet: FleetDep, bus: Events
    ) -> FleetReport:
        mine = await run_in_threadpool(_report, request, manager, fleet, bus)
        async with aggregate.client(settings.cluster_token) as http:
            return await aggregate.gather_reports(http, fleet.nodes(), local=mine)

    @fleet_view.get("/firewall", summary="Every node's firewall at once")
    async def fleet_firewall(
        rules: Rules, snapshots: SnapshotsDep, fleet: FleetDep
    ) -> FleetFirewall:
        async with aggregate.client(settings.cluster_token) as http:
            return await aggregate.gather_firewalls(
                http,
                fleet.nodes(),
                here=settings.node,
                local=firewall_status(rules, snapshots),
            )

    @fleet_view.get(
        "/firewall/check", summary="Whether one packet would get through, on every node"
    )
    async def fleet_firewall_check(
        rules: Rules,
        fleet: FleetDep,
        address: str,
        port: int,
        protocol: firewall.Protocol = firewall.Protocol.TCP,
        direction: firewall.Direction = firewall.Direction.IN,
    ) -> FleetVerdict:
        asked = _asked(address, port, protocol, direction)
        mine = NodeVerdict(node=settings.node, **_verdict(rules, asked).model_dump())
        params = {
            "address": asked.address,
            "port": str(asked.port),
            "protocol": asked.protocol.value,
            "direction": asked.direction.value,
        }
        async with aggregate.client(settings.cluster_token) as http:
            return await aggregate.gather_verdicts(
                http, fleet.nodes(), here=settings.node, local=mine, params=params
            )

    @fleet_view.get("/firewall/rules", summary="Every rule anywhere in the fleet")
    async def fleet_firewall_rules(
        rules: Rules, fleet: FleetDep, origin: str | None = None
    ) -> FleetRules:
        mine = [rule.model_dump(mode="json") for rule in rules.list(origin=origin)]
        async with aggregate.client(settings.cluster_token) as http:
            return await aggregate.gather_rules(
                http, fleet.nodes(), here=settings.node, local=mine, origin=origin
            )

    # Its own router, guarded like any other change: the cluster token reads and
    # announces, and forwarding a registration through the hub must not become
    # the one way it can write. The caller's own authorization goes with it.
    fleet_writes = APIRouter(
        prefix="/v1/fleet", tags=["fleet"], dependencies=[Depends(authorize)]
    )

    # Its own router, because a token that may register on a node is not
    # thereby a token that may change what crosses it.
    fleet_firewall_writes = APIRouter(
        prefix="/v1/fleet",
        tags=["fleet"],
        dependencies=[Depends(allowed(config.FIREWALL))],
        responses={status.HTTP_403_FORBIDDEN: {"model": ErrorResponse}},
    )

    @fleet_writes.post(
        "/services/{node}",
        summary="Register a service on one named node",
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {"description": "Registration renewed"},
            status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
        },
    )
    async def register_there(
        node: str,
        request: RegistrationRequest,
        manager: Manager,
        fleet: FleetDep,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> FleetRegistration:
        if node == settings.node:
            registration, created = manager.register(request)
            response.status_code = (
                status.HTTP_201_CREATED if created else status.HTTP_200_OK
            )
            return FleetRegistration(node=node, **registration.model_dump())
        async with aggregate.relaying(authorization) as http:
            registration, created = await aggregate.register_on(
                http,
                fleet.nodes(),
                node,
                request.model_dump(mode="json"),
                require_https=settings.require_https,
            )
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return registration

    @fleet_writes.post(
        "/services/{node}/{name}/heartbeat",
        summary="Extend a registration on one named node",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    async def heartbeat_there(
        node: str,
        name: str,
        request: HeartbeatRequest,
        manager: Manager,
        fleet: FleetDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> FleetRegistration:
        if node == settings.node:
            return FleetRegistration(node=node, **manager.heartbeat(name, request).model_dump())
        async with aggregate.relaying(authorization) as http:
            return await aggregate.heartbeat_on(
                http,
                fleet.nodes(),
                node,
                name,
                request.model_dump(mode="json"),
                require_https=settings.require_https,
            )

    @fleet_writes.delete(
        "/services/{node}/{name}",
        summary="Release a port on one named node",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    async def release_there(
        node: str,
        name: str,
        manager: Manager,
        fleet: FleetDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if node == settings.node:
            manager.release(name)
            return
        async with aggregate.relaying(authorization) as http:
            await aggregate.release_on(
                http, fleet.nodes(), node, name, require_https=settings.require_https
            )

    @fleet_writes.delete(
        "/listeners/{node}/{pid}",
        summary="Stop a process on one named node",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={
            status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
            status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        },
    )
    async def stop_there(
        node: str,
        pid: int,
        fleet: FleetDep,
        force: bool = False,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        # Asked by node because a pid means nothing off the machine it is on.
        # Each node keeps its own WARDEN_ALLOW_KILL; the hub opens nothing.
        if node == settings.node:
            stop_listener(pid, force=force)
            return
        async with aggregate.relaying(authorization) as http:
            await aggregate.stop_on(
                http,
                fleet.nodes(),
                node,
                pid,
                force=force,
                require_https=settings.require_https,
            )

    @reads.get("/update", summary="Whether a newer warden exists")
    def update_status(request: Request) -> UpdateStatus:
        return request.app.state.updates.status

    # Read from inside rather than over HTTP: the checks are written against a
    # client, and a warden making a request of itself to say how it is would be
    # the one machine in a fleet that reports on its own network instead of its
    # own state. Sync, so uvicorn keeps it off the loop - `_holders` walks the
    # machine's sockets and `_upstream` asks another machine entirely.
    @reads.get("/doctor", summary="What this warden has to say about itself")
    def doctor(request: Request, manager: Manager, fleet: FleetDep, bus: Events) -> Report:
        return _report(request, manager, fleet, bus)

    def _report(request: Request, manager: Registry, fleet: Fleet, bus: EventBus) -> Report:
        here = Here(settings, manager, fleet, bus, request.app.state.updates)
        checks = examine(here, settings)
        return Report(
            node=settings.node,
            worst=worst(checks),
            says=says(checks),
            checks=[Said(level=check.level, text=check.text) for check in checks],
        )

    # Changing one node's firewall from the hub. The node's own
    # `allow_remote_firewall` still decides, and its refusal comes back in its
    # own words - the hub adds nothing to the question and nothing to the answer.
    @fleet_firewall_writes.post(
        "/firewall/{node}/open",
        summary="Open a registered service's port on one named node",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    async def open_there(
        node: str,
        asked: OpenRequest,
        manager: Manager,
        rules: Rules,
        fleet: FleetDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        if node == settings.node:
            may_change_the_firewall()
            return {**firewall_open(asked, manager, rules).model_dump(mode="json"), "node": node}
        async with aggregate.relaying(authorization) as http:
            return await aggregate.open_on(
                http,
                fleet.nodes(),
                node,
                asked.model_dump(mode="json"),
                require_https=settings.require_https,
            )

    @fleet_firewall_writes.post(
        "/firewall/{node}/rules",
        summary="Write a rule down on one named node",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    async def write_there(
        node: str,
        asked: RuleRequest,
        rules: Rules,
        fleet: FleetDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        if node == settings.node:
            may_change_the_firewall()
            return {**firewall_write(asked, rules).model_dump(mode="json"), "node": node}
        async with aggregate.relaying(authorization) as http:
            return await aggregate.write_on(
                http,
                fleet.nodes(),
                node,
                asked.model_dump(mode="json"),
                require_https=settings.require_https,
            )

    @fleet_firewall_writes.delete(
        "/firewall/{node}/rules/{name}",
        summary="Take one rule back out on one named node",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    async def close_there(
        node: str,
        name: str,
        rules: Rules,
        fleet: FleetDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        if node == settings.node:
            may_change_the_firewall()
            return firewall_close(name, rules)
        async with aggregate.relaying(authorization) as http:
            await aggregate.close_on(
                http, fleet.nodes(), node, name, require_https=settings.require_https
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @fleet_firewall_writes.post(
        "/firewall/{node}/{what}",
        summary="Apply, confirm or restore on one named node",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    async def firewall_there(
        node: str,
        what: str,
        rules: Rules,
        snapshots: SnapshotsDep,
        fleet: FleetDep,
        rollback: int | None = None,
        snapshot: int | None = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        if what not in DOABLE:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no such thing to do: {what!r}; there is {', '.join(sorted(DOABLE))}",
            )
        if node == settings.node:
            return _here(what, rules, snapshots, rollback=rollback, snapshot=snapshot)
        params = {
            key: value
            for key, value in (("rollback", rollback), ("snapshot", snapshot))
            if value is not None
        }
        async with aggregate.relaying(authorization) as http:
            return await aggregate.firewall_on(
                http,
                fleet.nodes(),
                node,
                what,
                params=params,
                require_https=settings.require_https,
            )

    def _here(
        what: str,
        rules: Rules,
        snapshots: SnapshotsDep,
        *,
        rollback: int | None,
        snapshot: int | None,
    ) -> dict[str, object]:
        """The same three, on the warden the request arrived at."""
        may_change_the_firewall()
        if what == "apply":
            return firewall_apply(rules, snapshots, rollback)
        if what == "confirm":
            return firewall_confirm(snapshots)
        return firewall_restore(snapshots, snapshot)


    @fleet_firewall_writes.post(
        "/firewall/open",
        summary="Open a service on every node that holds it",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    async def open_everywhere(
        asked: OpenRequest,
        manager: Manager,
        rules: Rules,
        fleet: FleetDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> FleetFirewallResult:
        """A name means a different port on every machine, and none on most.

        Which nodes hold it is asked first, so a node that never registered
        this service is skipped and told so rather than asked for something it
        has nothing to open for.
        """
        nodes = fleet.nodes()
        async with aggregate.client(settings.cluster_token) as looking:
            everything = await aggregate.gather_services(
                looking, nodes, here=settings.node, local=manager.list()
            )
        holders = {
            service.node for service in everything.services if service.name == asked.service
        }
        if not holders:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no node in the fleet holds a service called {asked.service!r}",
            )

        async with aggregate.relaying(authorization) as http:
            found = await aggregate.open_where_held(
                http,
                nodes,
                asked.service,
                asked.model_dump(mode="json"),
                here=settings.node,
                holders=holders,
                require_https=settings.require_https,
            )
        if settings.node in holders:
            found.results = [one for one in found.results if one.node != settings.node]
            found.results.append(_open_here(asked, manager, rules))
            found.results.sort(key=lambda result: result.node)
        return found

    def _open_here(asked: OpenRequest, manager: Manager, rules: Rules) -> FirewallResult:
        """This warden's own answer, through the same door and the same gate."""
        try:
            may_change_the_firewall()
            opened = firewall_open(asked, manager, rules)
        except WardenError as exc:
            return FirewallResult(
                node=settings.node, url=settings.advertise_url, ok=False, detail=exc.message
            )
        return FirewallResult(
            node=settings.node,
            url=settings.advertise_url,
            ok=True,
            detail=aggregate._opened(opened.model_dump(mode="json")),
        )

    @fleet_firewall_writes.post(
        "/firewall/{what}",
        summary="Apply, confirm or restore across the whole fleet",
        responses={
            status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
            status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        },
    )
    async def firewall_everywhere(
        what: str,
        rules: Rules,
        snapshots: SnapshotsDep,
        fleet: FleetDep,
        rollback: int | None = None,
        snapshot: int | None = None,
    ) -> FleetFirewallResult:
        """Every node does it to its own firewall, and answers for itself.

        A fleet-wide apply without a rollback is refused. It is the one place
        warden will not let the window be left out: a rule that shuts the door
        shuts it on every machine at once, and the watchdog on each of them is
        the only thing that opens it again without somebody driving there.
        """
        if what not in DOABLE:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no such thing to do: {what!r}; there is {', '.join(sorted(DOABLE))}",
            )
        seconds = settings.firewall_rollback if rollback is None else rollback
        if what == "apply" and seconds <= 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "a fleet-wide apply has to keep its rollback - one wrong rule "
                "would otherwise shut every machine at once, with nothing left "
                "to open them again",
            )

        try:
            here = FirewallResult(
                node=settings.node,
                url=settings.advertise_url,
                ok=True,
                detail=aggregate._said(
                    what, _here(what, rules, snapshots, rollback=seconds, snapshot=snapshot)
                ),
            )
        except WardenError as exc:
            here = FirewallResult(
                node=settings.node, url=settings.advertise_url, ok=False, detail=exc.message
            )

        params: dict[str, object] = {"rollback": seconds} if what == "apply" else {}
        if snapshot is not None:
            params["snapshot"] = snapshot
        async with aggregate.client(settings.cluster_token, timeout=30.0) as http:
            return await aggregate.firewall_fleet(
                http, fleet.nodes(), what, params=params, here=here
            )


    # Its own router: on `v1` the blanket person-check would run first and turn
    # a hub's perfectly good cluster token into a 401.
    between = APIRouter(prefix="/v1", tags=["updates"], dependencies=[Depends(known_caller)])

    @between.post(
        "/update",
        summary="Ask this warden to update itself",
        responses={
            status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
        },
    )
    def update_self() -> dict[str, str]:
        # What updating means lives in this machine's own configuration. The
        # request carries no command, so a hub can ask but never dictate.
        return {"detail": updates.apply(settings)}

    @fleet_view.post("/update", summary="Ask every warden in the fleet to update itself")
    async def update_everyone(fleet: FleetDep) -> FleetUpdate:
        try:
            here = UpdateResult(
                node=settings.node,
                url=settings.advertise_url,
                ok=True,
                detail=updates.apply(settings),
            )
        except WardenError as exc:
            here = UpdateResult(
                node=settings.node, url=settings.advertise_url, ok=False, detail=exc.message
            )
        async with aggregate.client(settings.cluster_token, timeout=300.0) as http:
            return await aggregate.update_fleet(http, fleet.nodes(), here=here)

    def may_change_the_firewall() -> None:
        """The switch that has to be thrown before a caller may touch the rules.

        Separate from the bounds in firewall/bounds.py, which decide what a
        rule may be. This decides whether anybody over the network may ask at
        all - and off is the answer a machine gives until somebody says so.
        """
        if not settings.allow_remote_firewall:
            raise NotPermittedError(
                "changing the firewall over the API is switched off - "
                "set allow_remote_firewall on this warden to allow it"
            )

    firewall_reads = APIRouter(
        prefix="/v1/firewall", tags=["firewall"], dependencies=[Depends(known_caller)]
    )
    firewall_writes = APIRouter(
        prefix="/v1/firewall",
        tags=["firewall"],
        dependencies=[
            Depends(allowed(config.FIREWALL)),
            Depends(may_change_the_firewall),
        ],
        responses={status.HTTP_403_FORBIDDEN: {"model": ErrorResponse}},
    )

    def _backend() -> base.Backend:
        return base.backend_for(settings.firewall_backend)

    @firewall_reads.get("", summary="What this machine's firewall is")
    def firewall_status(rules: Rules, snapshots: SnapshotsDep) -> FirewallStatus:
        backend = _backend()
        held = rules.list()
        waiting = guard.armed(snapshots)
        drifted = guard.pending(held, snapshots)
        return FirewallStatus(
            backend=backend.kind,
            available=backend.available(),
            enabled=settings.firewall_from_registry,
            remote=settings.allow_remote_firewall,
            rules=len(held),
            live=len(firewall.Policy(rules=held).live(datetime.now(UTC))),
            from_registry=sum(1 for rule in held if rule.origin is firewall.Origin.REGISTRY),
            rollback_at=waiting.deadline if waiting else None,
            pending=drifted.count,
            applied_at=drifted.applied_at,
        )

    @firewall_reads.get("/rules", summary="Every rule this machine holds")
    def firewall_rules(rules: Rules, origin: str | None = None) -> list[firewall.Rule]:
        return rules.list(origin=origin)

    @firewall_reads.get(
        "/check",
        summary="Whether one packet would get through, and which rule decides",
        responses={status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": ErrorResponse}},
    )
    def firewall_check(
        rules: Rules,
        address: str,
        port: int,
        protocol: firewall.Protocol = firewall.Protocol.TCP,
        direction: firewall.Direction = firewall.Direction.IN,
    ) -> Verdict:
        return _verdict(rules, _asked(address, port, protocol, direction))

    def _asked(
        address: str, port: int, protocol: firewall.Protocol, direction: firewall.Direction
    ) -> firewall.Asked:
        try:
            return firewall.Asked(
                address=address, port=port, protocol=protocol, direction=direction
            )
        except ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    def _verdict(rules: RuleStore, asked: firewall.Asked) -> Verdict:
        policy = firewall.Policy(rules=rules.list())
        found = firewall.decides(policy, asked, datetime.now(UTC))
        return Verdict(
            address=asked.address,
            port=asked.port,
            protocol=asked.protocol.value,
            direction=asked.direction.value,
            action=found.action.value,
            rule=found.rule.name if found.rule else None,
            why=found.why,
            passed_over=list(found.passed_over),
        )

    @firewall_writes.post(
        "/open",
        summary="Open the port a registered service holds",
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    def firewall_open(asked: OpenRequest, manager: Manager, rules: Rules) -> firewall.Rule:
        # Through the same door `warden firewall open` uses, so the eight
        # bounds are not something the API can be talked round.
        service = link.found(manager.list(), asked.service)
        rule = link.rule_for(
            service,
            source=asked.source or _only_network(),
            settings=settings,
            comment=asked.comment,
        )
        rules.save(rule)
        return rule

    def _only_network() -> str:
        """The network to open to when the caller named none.

        One declared network is not a choice, so warden makes it. More than one
        is, and a caller who did not make it does not get one picked for them.
        """
        allowed = sorted(settings.firewall_allow_from)
        if len(allowed) == 1:
            return allowed[0]
        raise NotPermittedError(
            "name the network to open to - this machine allows "
            + (", ".join(allowed) if allowed else "none")
        )

    @firewall_writes.post(
        "/rules",
        summary="Write a rule down by hand",
        responses={status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": ErrorResponse}},
    )
    def firewall_write(asked: RuleRequest, rules: Rules) -> firewall.Rule:
        # Through `catalogue.rule_for`, the same words the command line reads,
        # so `ssh` means the same thing typed as it does asked for.
        try:
            rule = catalogue.rule_for(
                asked.what,
                action=firewall.Action(asked.action),
                source=asked.source,
                direction=firewall.Direction(asked.direction),
                protocol=asked.protocol,
                comment=asked.comment,
                limit=asked.limit,
            )
        except ValidationError as exc:
            # The rule is where the fields are really checked, so a refusal
            # there is the caller's mistake and not this warden's failure.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, exc.errors()[0]["msg"]
            ) from exc
        rules.save(rule)
        return rule

    @firewall_writes.delete(
        "/rules/{name}",
        summary="Take one rule back out",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    )
    def firewall_close(name: str, rules: Rules) -> Response:
        if not rules.delete(name):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no rule called {name!r}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @firewall_writes.post("/apply", summary="Make the rules true on this machine")
    def firewall_apply(rules: Rules, snapshots: SnapshotsDep, rollback: int | None = None) -> dict:
        # A rollback matters more over the API than at a keyboard: the caller
        # is somewhere else, and a rule that shuts the door shuts it on them.
        seconds = settings.firewall_rollback if rollback is None else rollback
        policy = firewall.Policy(rules=rules.list())
        backend = _backend()
        waiting = guard.apply(backend, snapshots, policy, rollback=seconds)
        if waiting is not None:
            guard.start_watchdog(str(settings.database), waiting.deadline)
        return {
            "applied": len(policy.live(datetime.now(UTC))),
            "rollback_at": waiting.deadline.isoformat() if waiting else None,
        }

    @firewall_writes.post("/confirm", summary="Keep what was applied")
    def firewall_confirm(snapshots: SnapshotsDep) -> dict:
        kept = guard.confirm(snapshots)
        return {"confirmed": kept.snapshot}

    @firewall_writes.post("/restore", summary="Put the last snapshot back")
    def firewall_restore(snapshots: SnapshotsDep, snapshot: int | None = None) -> dict:
        return {"restored": guard.roll_back(_backend(), snapshots, snapshot)}


    app.include_router(reads)
    app.include_router(between)
    app.include_router(v1)
    app.include_router(nodes)
    app.include_router(fleet_view)
    app.include_router(fleet_writes)
    app.include_router(fleet_firewall_writes)
    app.include_router(firewall_reads)
    app.include_router(firewall_writes)

    @app.get(
        "/metrics",
        summary="Prometheus metrics",
        tags=["meta"],
        dependencies=[Depends(known_caller)],
        response_class=PlainTextResponse,
    )
    def prometheus(
        manager: Manager, fleet: FleetDep, rules: Rules, snapshots: SnapshotsDep
    ) -> Response:
        """Behind the same token as every other read.

        Left open on a warden bound to 0.0.0.0 this would hand out the shape of
        the whole fleet to anyone who asked.
        """
        return PlainTextResponse(
            metrics.render(
                pool=manager.pool_status(),
                services=manager.list(),
                nodes=fleet.nodes(),
                version=__version__,
                node=settings.node,
                role=settings.role,
                walls=_walls(rules, snapshots),
            ),
            media_type=metrics.CONTENT_TYPE,
        )

    def _walls(rules: Rules, snapshots: SnapshotsDep) -> metrics.Walls:
        """The firewall's numbers, all of them out of the store."""
        held = rules.list()
        counted = snapshots.tallies()
        return metrics.Walls(
            by_origin=dict(Counter(str(rule.origin) for rule in held)),
            live=len(firewall.Policy(rules=held).live(datetime.now(UTC))),
            pending=guard.pending(held, snapshots).count,
            rollback_armed=guard.armed(snapshots) is not None,
            applied=counted.get(guard.APPLIED, 0),
            rolled_back=counted.get(guard.ROLLED_BACK, 0),
        )

    @app.get("/health", summary="Liveness probe", tags=["meta"])
    def health(manager: Manager, fleet: FleetDep) -> Health:
        return Health(
            status="ok",
            version=__version__,
            node=settings.node,
            role=settings.role,
            services=manager.store.count(),
            nodes=fleet.count(),
        )

    return app

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from types import TracebackType
from typing import Any

import httpx

from warden.core.config import DEFAULT_URL
from warden.errors import (
    NotPermittedError,
    PoolExhaustedError,
    PortUnavailableError,
    UnknownServiceError,
    WardenError,
)
from warden.firewall.model import Rule
from warden.models import (
    Event,
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
    Health,
    Listener,
    Node,
    PoolStatus,
    Registration,
    Report,
    UpdateStatus,
    WebhookStatus,
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

    def register_group(
        self,
        name: str,
        *,
        kind: str,
        count: int,
        contiguous: bool = False,
        project: str | None = None,
        host: str = "127.0.0.1",
        pid: int | None = None,
        ttl: int | None = None,
        meta: dict[str, str] | None = None,
    ) -> list[Registration]:
        """Several ports for one thing, named ``name-1`` upwards.

        Chosen and written together, so the set is either wholly held or not
        held at all - which is the part a caller asking four times cannot do.
        """
        payload = {
            "name": name,
            "kind": kind,
            "count": count,
            "contiguous": contiguous,
            "project": project,
            "host": host,
            "pid": pid,
            "ttl": ttl,
            "meta": meta or {},
        }
        return [
            Registration.model_validate(item)
            for item in self._request("POST", "/v1/groups", json=payload)
        ]

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

    def webhook(self) -> WebhookStatus:
        """Where this warden posts events, and how that has been going."""
        return WebhookStatus.model_validate(self._request("GET", "/v1/webhook"))

    def events(self) -> Iterator[Event]:
        """Every change as it happens, until the caller stops reading.

        No timeout: the whole point is a connection that stays open through the
        long quiet stretches where nothing is registered at all.
        """
        try:
            with self._http.stream("GET", "/v1/events", timeout=None) as response:
                if response.is_error:
                    response.read()
                    raise _STATUS_ERRORS.get(response.status_code, WardenError)(
                        detail_of(response)
                    )
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        yield Event.model_validate_json(line[len("data:") :].strip())
        except httpx.ConnectError as exc:
            raise WardenError(
                f"no warden reachable at {self.url} - start one with 'warden serve'"
            ) from exc

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

    def firewall(self) -> FirewallStatus:
        """What that machine's firewall is, and whether it is about to undo itself."""
        return FirewallStatus.model_validate(self._request("GET", "/v1/firewall"))

    def firewall_rules(self, *, origin: str | None = None) -> list[Rule]:
        """Every rule it holds, and where each one came from."""
        params = {"origin": origin} if origin else None
        said = self._request("GET", "/v1/firewall/rules", params=params)
        return [Rule.model_validate(rule) for rule in said]

    def firewall_open(
        self, service: str, *, source: str = "", comment: str | None = None
    ) -> Rule:
        """Let through the port a registered service holds.

        A name rather than a port: the registry knows which port that is and
        how long it holds it for, so the rule inherits the lease and closes
        when the service does. Bounded by the pool, by firewall_allow_from, and
        by every other rule in firewall/bounds.py - and refused outright unless
        that machine has allow_remote_firewall set.
        """
        body = {"service": service, "source": source, "comment": comment}
        return Rule.model_validate(self._request("POST", "/v1/firewall/open", json=body))

    def firewall_write(
        self,
        what: str,
        *,
        action: str = "allow",
        source: str = "any",
        direction: str = "in",
        protocol: str | None = None,
        comment: str | None = None,
        limit: str | None = None,
        node: str | None = None,
    ) -> dict[str, object]:
        """Write a rule down by hand: a port, a range, or a catalogue name.

        Not bounded by the pool - those bounds are about what the registry may
        ask for. This is a token holder saying so, and `allow_remote_firewall`
        on the machine being asked is what allows it at all.
        """
        body = {
            "what": what,
            "action": action,
            "source": source,
            "direction": direction,
            "protocol": protocol,
            "comment": comment,
            "limit": limit,
        }
        where = f"/v1/fleet/firewall/{node}/rules" if node else "/v1/firewall/rules"
        return dict(self._request("POST", where, json=body))

    def firewall_close(self, name: str) -> None:
        """Take one rule back out."""
        self._request("DELETE", f"/v1/firewall/rules/{name}")

    def firewall_apply(self, *, rollback: int | None = None) -> dict[str, object]:
        """Make the rules true on that machine, with a rollback armed.

        Nothing is kept until `firewall_confirm`. A caller on another machine
        that shuts the door on itself gets it opened again by the watchdog.
        """
        params = {"rollback": rollback} if rollback is not None else None
        return dict(self._request("POST", "/v1/firewall/apply", params=params))

    def firewall_confirm(self) -> int:
        """Keep what was applied, and stop the rollback that is waiting."""
        return int(self._request("POST", "/v1/firewall/confirm")["confirmed"])

    def firewall_restore(self, snapshot: int | None = None) -> int:
        """Put a snapshot back, whether or not one was waiting."""
        params = {"snapshot": snapshot} if snapshot is not None else None
        return int(self._request("POST", "/v1/firewall/restore", params=params)["restored"])

    def doctor(self) -> Report:
        """What that warden has to say about itself, examined where it runs."""
        return Report.model_validate(self._request("GET", "/v1/doctor"))

    def fleet_doctor(self) -> FleetReport:
        """The same from every node, each having examined itself."""
        return FleetReport.model_validate(self._request("GET", "/v1/fleet/doctor"))

    def fleet_firewall(self) -> FleetFirewall:
        """Every node's firewall at once, and the ones that did not answer."""
        return FleetFirewall.model_validate(self._request("GET", "/v1/fleet/firewall"))

    def fleet_firewall_rules(self, *, origin: str | None = None) -> FleetRules:
        """Every rule anywhere in the fleet, each carrying the node it is on."""
        params = {"origin": origin} if origin else None
        return FleetRules.model_validate(
            self._request("GET", "/v1/fleet/firewall/rules", params=params)
        )

    def firewall_open_on(
        self, node: str, service: str, *, source: str = "", comment: str | None = None
    ) -> dict[str, object]:
        """Ask one node in the fleet to open a service of its own.

        The port and the lease are that node's answer, and the bounds are
        checked there. Needs `allow_remote_firewall` on the node, not the hub.
        """
        body = {"service": service, "source": source, "comment": comment}
        return dict(self._request("POST", f"/v1/fleet/firewall/{node}/open", json=body))

    def firewall_close_on(self, node: str, name: str) -> None:
        """Take one rule back out on one node."""
        self._request("DELETE", f"/v1/fleet/firewall/{node}/rules/{name}")

    def firewall_apply_on(self, node: str, *, rollback: int | None = None) -> dict[str, object]:
        """Make one node's rules true, with its own rollback armed."""
        params = {"rollback": rollback} if rollback is not None else None
        return dict(self._request("POST", f"/v1/fleet/firewall/{node}/apply", params=params))

    def firewall_confirm_on(self, node: str) -> dict[str, object]:
        """Keep what one node applied."""
        return dict(self._request("POST", f"/v1/fleet/firewall/{node}/confirm"))

    def firewall_restore_on(
        self, node: str, snapshot: int | None = None
    ) -> dict[str, object]:
        """Put a snapshot back on one node."""
        params = {"snapshot": snapshot} if snapshot is not None else None
        return dict(self._request("POST", f"/v1/fleet/firewall/{node}/restore", params=params))

    def firewall_open_everywhere(
        self, service: str, *, source: str = "", comment: str | None = None
    ) -> FleetFirewallResult:
        """Open a service on every node in the fleet that holds it.

        A name is a different port on every machine. Nodes that never
        registered it are named as skipped rather than counted as failures.
        """
        body = {"service": service, "source": source, "comment": comment}
        return FleetFirewallResult.model_validate(
            self._request("POST", "/v1/fleet/firewall/open", json=body, timeout=60.0)
        )

    def firewall_apply_fleet(self, *, rollback: int | None = None) -> FleetFirewallResult:
        """Make every node's rules true, each with its own rollback armed.

        A fleet-wide apply keeps its window: pass `rollback=0` and it is
        refused. Nothing is kept until `firewall_confirm_fleet`, and a node
        that is never confirmed puts itself back on its own.
        """
        params = {"rollback": rollback} if rollback is not None else None
        return FleetFirewallResult.model_validate(
            self._request("POST", "/v1/fleet/firewall/apply", params=params, timeout=60.0)
        )

    def firewall_confirm_fleet(self) -> FleetFirewallResult:
        """Keep what every node applied."""
        return FleetFirewallResult.model_validate(
            self._request("POST", "/v1/fleet/firewall/confirm", timeout=60.0)
        )

    def firewall_restore_fleet(self, snapshot: int | None = None) -> FleetFirewallResult:
        """Put every node back to a snapshot, whether or not one was waiting."""
        params = {"snapshot": snapshot} if snapshot is not None else None
        return FleetFirewallResult.model_validate(
            self._request("POST", "/v1/fleet/firewall/restore", params=params, timeout=60.0)
        )

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

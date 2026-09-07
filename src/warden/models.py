from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

Name = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$", strip_whitespace=True),
]
Kind = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$", strip_whitespace=True),
]
Project = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$", strip_whitespace=True),
]
Port = Annotated[int, Field(ge=1, le=65535)]

# Text that ends up in something which parses what it is given: a Caddyfile, an
# nginx server block, an nftables comment, a chat message. A quote or a
# backslash ends a quoted string somewhere, and a control character ends a
# line - either is a way to write something the person generating it did not.
UNQUOTABLE = '"' + chr(92)


def plain(value: str) -> str:
    """Text safe to write into a file that will be parsed. Raises if it is not."""
    if not value.isprintable():
        raise ValueError("cannot contain control characters or line breaks")
    if any(character in UNQUOTABLE for character in value):
        raise ValueError("cannot contain quotes or backslashes")
    return value


Plain = Annotated[str, AfterValidator(plain)]

MetaKey = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", strip_whitespace=True)
]
MetaValue = Annotated[str, Field(max_length=255), AfterValidator(plain)]


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    kind: Kind
    project: Project | None = None
    host: str = "127.0.0.1"
    preferred_port: Port | None = None
    require_port: Port | None = None
    pid: int | None = Field(default=None, ge=1)
    ttl: int | None = Field(default=None, ge=1, le=86_400)
    meta: dict[MetaKey, MetaValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_wish_at_a_time(self) -> RegistrationRequest:
        if self.preferred_port is not None and self.require_port is not None:
            raise ValueError("set either preferred_port or require_port, not both")
        return self


class GroupRequest(BaseModel):
    """Several ports for one thing, asked for in one go.

    No port wishes here. `require_port` for a group of four has no sensible
    answer, and a caller who needs one particular port needs one registration.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name
    kind: Kind
    count: int = Field(ge=1, le=64)
    contiguous: bool = False
    project: Project | None = None
    host: str = "127.0.0.1"
    pid: int | None = Field(default=None, ge=1)
    ttl: int | None = Field(default=None, ge=1, le=86_400)
    meta: dict[str, str] = Field(default_factory=dict)

    @property
    def members(self) -> list[str]:
        return [f"{self.name}-{index}" for index in range(1, self.count + 1)]

    @model_validator(mode="after")
    def _members_can_be_named(self) -> GroupRequest:
        longest = self.members[-1]
        if len(longest) > 64:
            raise ValueError(f"{longest!r} is too long a name for a service")
        return self


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int | None = Field(default=None, ge=1)
    ttl: int | None = Field(default=None, ge=1, le=86_400)


class Registration(BaseModel):
    name: str
    kind: str
    project: str | None
    host: str
    port: int
    pid: int | None
    meta: dict[str, str]
    ttl: int | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    # Both only filled when they were asked for; the sweep costs a syscall.
    holder: str | None = None
    holder_reason: str | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class Listener(BaseModel):
    """A socket bound on this machine, whether warden handed it out or not."""

    protocol: str
    host: str
    port: int
    pid: int | None
    process: str | None
    user: str | None
    started_at: datetime | None
    command: str | None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class PoolStatus(BaseModel):
    start: int
    end: int
    size: int
    reserved: list[int]
    allocated: int
    available: int
    # What the registry knows, not what a probe would say: the longest stretch
    # of free ports in a row, which is what a contiguous request needs.
    largest_run: int = 0


class NodeAnnouncement(BaseModel):
    """A warden telling a hub that it exists and what it hands out."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    url: str
    pool_start: Port
    pool_end: Port
    version: str

    @field_validator("url")
    @classmethod
    def _must_be_an_address(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must be an http address, for example http://build-01:7010")
        return value.rstrip("/")


class Node(BaseModel):
    name: str
    url: str
    pool_start: int
    pool_end: int
    version: str
    first_seen: datetime
    last_seen: datetime
    expires_at: datetime

    @computed_field
    @property
    def status(self) -> str:
        """A node that stopped reporting is shown as stale, never quietly dropped."""
        return "online" if self.expires_at > datetime.now(UTC) else "stale"

    @property
    def pool(self) -> str:
        return f"{self.pool_start}-{self.pool_end}"


PORT = "port"
NODE = "node"
FIREWALL = "firewall"

SCOPES = (PORT, NODE, FIREWALL)


class Event(BaseModel):
    """Something that happened, kept after it stopped being true.

    `scope` says what kind of thing it happened to and `action` what happened.
    They are two fields rather than one string so that a reader written against
    0.2.0, which only knew about ports and matched on `action`, keeps working.

    The port fields stay where they were for the same reason. Anything a scope
    needs that they cannot hold goes in `body`, which is why a node joining and
    a ruleset being applied both fit without the shape changing again.
    """

    at: datetime
    scope: str = PORT
    action: str
    subject: str = ""
    name: str = ""
    kind: str = ""
    project: str | None = None
    host: str = ""
    port: int = 0
    pid: int | None = None
    body: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _named_one_way_or_the_other(self) -> Event:
        """A port event names itself in `name`; everything else in `subject`."""
        if not self.subject and self.name:
            object.__setattr__(self, "subject", self.name)
        return self

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def full(self) -> str:
        """`port.registered`, `node.stale` - the name a filter can be written to."""
        return f"{self.scope}.{self.action}"

class WebhookStatus(BaseModel):
    """What became of the events this warden tried to post.

    The address is cut back to its host on purpose. A webhook URL is a
    credential, and its path is the half worth stealing.
    """

    configured: bool
    target: str | None = None
    format: str | None = None
    actions: list[str] = Field(default_factory=list)
    watching: int = 0
    delivered: int = 0
    failed: int = 0
    dropped: int = 0
    last_error: str | None = None
    last_sent: datetime | None = None


class Health(BaseModel):
    """What a warden says about itself when asked whether it is there."""

    status: str
    version: str
    node: str
    role: str
    services: int
    nodes: int


class FirewallStatus(BaseModel):
    """What this machine's firewall is, and whether it is about to undo itself."""

    backend: str
    available: bool
    enabled: bool
    remote: bool
    rules: int
    live: int
    from_registry: int
    rollback_at: datetime | None = None


class OpenRequest(BaseModel):
    """Ask for the port a registered service holds to be let through.

    A service name rather than a port: the registry knows which port that is
    and how long it holds it for, and a caller naming a number could name any
    number. What comes back still has to pass every bound in firewall/bounds.py.
    """

    service: Name
    source: str = ""
    comment: Plain | None = None


class RuleRequest(BaseModel):
    """Write a rule down by hand, the way `warden firewall allow` does.

    Not bounded by the pool or by `firewall_allow_from`: those bound what the
    *registry* may ask for, and this is somebody with the token saying so. What
    gates it is `allow_remote_firewall`, which is off until a machine says
    otherwise - and `warden doctor` fails a machine that says otherwise while
    listening beyond loopback with no token at all.
    """

    what: Name
    action: Literal["allow", "deny", "reject"] = "allow"
    source: str = "any"
    direction: Literal["in", "out"] = "in"
    protocol: Literal["tcp", "udp", "icmp", "any"] | None = None
    comment: Plain | None = None


class NodeFirewall(FirewallStatus):
    """One node's firewall, and whose it is."""

    node: str


class FleetFirewall(BaseModel):
    """Every node's firewall, and the ones that did not answer.

    Nothing is summed. Two nodes with a rule apiece do not have two rules
    between them in any sense that matters - each machine decides for itself
    what may cross it.
    """

    firewalls: list[NodeFirewall]
    unreachable: list[Unreachable]


class FleetRules(BaseModel):
    """Every rule anywhere in the fleet, and where it was not possible to look.

    The rules are typed loosely on purpose: what a node sends back is its own
    rule, and a hub that insisted on parsing it would refuse to show a fleet
    running a newer warden than itself.
    """

    rules: list[dict[str, object]]
    unreachable: list[Unreachable]


class FirewallResult(BaseModel):
    """What happened when one node was asked to do something to its firewall."""

    node: str
    url: str
    ok: bool
    detail: str


class FleetFirewallResult(BaseModel):
    """One line per node, whichever way it went."""

    results: list[FirewallResult]

    @property
    def kept(self) -> int:
        return sum(1 for result in self.results if result.ok)


class UpdateStatus(BaseModel):
    """Whether a newer warden exists, or why that is not known."""

    current: str
    latest: str | None = None
    available: bool = False
    url: str | None = None
    checked_at: datetime | None = None
    reason: str | None = None


class UpdateResult(BaseModel):
    """What happened when one warden was asked to update itself."""

    node: str
    url: str
    ok: bool
    detail: str


class FleetUpdate(BaseModel):
    results: list[UpdateResult]


class FleetRegistration(Registration):
    """A registration, and which warden handed it out."""

    node: str


class Unreachable(BaseModel):
    """A node the hub could not get an answer from, and why."""

    node: str
    url: str
    reason: str


class Duplicate(BaseModel):
    """A name more than one node hands out."""

    name: str
    nodes: list[str]


class FleetServices(BaseModel):
    """What the fleet holds, and what could not be asked.

    The two are separate on purpose. A shorter list because a machine was down
    reads exactly like a shorter list because a service was released, and those
    are not the same thing at all.
    """

    services: list[FleetRegistration]
    unreachable: list[Unreachable]
    duplicates: list[Duplicate] = Field(default_factory=list)


class FleetListener(Listener):
    """A socket, and the machine it is bound on."""

    node: str


class FleetListeners(BaseModel):
    """Every socket the fleet reports, and the machines that did not report."""

    listeners: list[FleetListener]
    unreachable: list[Unreachable]


class NodePool(PoolStatus):
    """One node's pool, and whose it is."""

    node: str


class FleetPool(BaseModel):
    """Every node's pool, and what the fleet has left altogether.

    The totals count ports that may actually be handed out, so a range with
    half of it reserved does not read as capacity anyone can have.
    """

    pools: list[NodePool]
    unreachable: list[Unreachable]

    @computed_field
    @property
    def allocated(self) -> int:
        return sum(pool.allocated for pool in self.pools)

    @computed_field
    @property
    def available(self) -> int:
        return sum(pool.available for pool in self.pools)

    @computed_field
    @property
    def capacity(self) -> int:
        return sum(pool.allocated + pool.available for pool in self.pools)


class ErrorResponse(BaseModel):
    detail: str

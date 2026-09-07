from __future__ import annotations

from datetime import timedelta

from warden.core.config import insecure
from warden.core.store import Store
from warden.errors import NodeMovedError, NotPermittedError, UnknownNodeError
from warden.models import NODE, Node, NodeAnnouncement
from warden.ports.service import utcnow

# What can happen to a node, as far as anybody watching is concerned.
JOINED = "joined"
RETURNED = "returned"
STALE = "stale"
FORGOTTEN = "forgotten"


class Fleet:
    """The other wardens this one knows about.

    A node that stops reporting is kept and shown as stale rather than dropped.
    Silently forgetting a server is worse than showing one that is not answering:
    the second is a fact to act on, the first looks like it was never there.
    """

    def __init__(self, store: Store, *, ttl: int = 90, require_https: bool = False) -> None:
        self.store = store
        self.ttl = ttl
        self.require_https = require_https
        # Which ones have already been reported quiet, so it is said once.
        self._said_stale: set[str] = set()

    def announce(self, announcement: NodeAnnouncement) -> tuple[Node, bool]:
        """Record a node, or refresh what is known about it."""
        now = utcnow()
        existing = self.store.get_node(announcement.name)
        self._allowed(announcement, existing)
        node = Node(
            name=announcement.name,
            url=announcement.url,
            pool_start=announcement.pool_start,
            pool_end=announcement.pool_end,
            version=announcement.version,
            first_seen=existing.first_seen if existing else now,
            last_seen=now,
            expires_at=now + timedelta(seconds=self.ttl),
        )
        self.store.save_node(node)
        if existing is None:
            self.store.announce(
                NODE, JOINED, node.name, url=node.url, pool=node.pool, version=node.version
            )
        elif existing.status == "stale":
            # Worth hearing about on its own: a machine that went quiet and came
            # back is a different story from one that was never away.
            self.store.announce(
                NODE, RETURNED, node.name, url=node.url, away_since=existing.last_seen
            )
        return node, existing is None

    def _allowed(self, announcement: NodeAnnouncement, existing: Node | None) -> None:
        """Whether this announcement may take the name it is asking for.

        A name is pinned to the address it first arrived with. Anyone holding
        the cluster token could otherwise re-announce an existing node at an
        address of their own, and the hub would forward the next person's token
        straight to it.
        """
        if self.require_https and insecure(announcement.url):
            raise NotPermittedError(
                f"{announcement.url} is plain HTTP and this warden requires HTTPS; "
                "a token sent there would cross the network in the clear"
            )
        if existing and existing.url != announcement.url:
            raise NodeMovedError(
                f"{announcement.name} is already at {existing.url} and now claims "
                f"{announcement.url}. If it really moved, "
                f"`warden nodes --forget {announcement.name}` first"
            )

    def nodes(self) -> list[Node]:
        known = self.store.list_nodes()
        self._notice_the_quiet_ones(known)
        return known

    def _notice_the_quiet_ones(self, known: list[Node]) -> None:
        """Say once when a node stops answering, not on every listing.

        A node going quiet is the thing somebody wants told; the same node
        still being quiet an hour later is not.
        """
        gone = {node.name for node in known if node.status == "stale"}
        for name in sorted(gone - self._said_stale):
            node = next(one for one in known if one.name == name)
            self.store.announce(NODE, STALE, name, url=node.url, last_seen=node.last_seen)
        self._said_stale = gone

    def get(self, name: str) -> Node:
        node = self.store.get_node(name)
        if node is None:
            raise UnknownNodeError(f"no node registered as {name!r}")
        return node

    def forget(self, name: str) -> None:
        if not self.store.delete_node(name):
            raise UnknownNodeError(f"no node registered as {name!r}")
        self._said_stale.discard(name)
        self.store.announce(NODE, FORGOTTEN, name)

    def count(self) -> int:
        return self.store.count_nodes()

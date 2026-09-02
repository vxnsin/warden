from __future__ import annotations

from datetime import timedelta

from warden.config import insecure
from warden.errors import NodeMovedError, NotPermittedError, UnknownNodeError
from warden.models import Node, NodeAnnouncement
from warden.service import utcnow
from warden.store import Store


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
        return self.store.list_nodes()

    def get(self, name: str) -> Node:
        node = self.store.get_node(name)
        if node is None:
            raise UnknownNodeError(f"no node registered as {name!r}")
        return node

    def forget(self, name: str) -> None:
        if not self.store.delete_node(name):
            raise UnknownNodeError(f"no node registered as {name!r}")

    def count(self) -> int:
        return self.store.count_nodes()

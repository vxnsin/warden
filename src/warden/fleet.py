from __future__ import annotations

from datetime import timedelta

from warden.errors import UnknownNodeError
from warden.models import Node, NodeAnnouncement
from warden.service import utcnow
from warden.store import Store


class Fleet:
    """The other wardens this one knows about.

    A node that stops reporting is kept and shown as stale rather than dropped.
    Silently forgetting a server is worse than showing one that is not answering:
    the second is a fact to act on, the first looks like it was never there.
    """

    def __init__(self, store: Store, *, ttl: int = 90) -> None:
        self.store = store
        self.ttl = ttl

    def announce(self, announcement: NodeAnnouncement) -> tuple[Node, bool]:
        """Record a node, or refresh what is known about it."""
        now = utcnow()
        existing = self.store.get_node(announcement.name)
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

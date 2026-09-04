from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from warden.models import Event, Node, Registration

logger = logging.getLogger("warden.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations (
    name       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    project    TEXT,
    host       TEXT NOT NULL,
    port       INTEGER NOT NULL,
    pid        INTEGER,
    meta       TEXT NOT NULL DEFAULT '{}',
    ttl        INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS registrations_endpoint
    ON registrations (host, port);
CREATE INDEX IF NOT EXISTS registrations_project
    ON registrations (project);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    action  TEXT NOT NULL,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    project TEXT,
    host    TEXT NOT NULL,
    port    INTEGER NOT NULL,
    pid     INTEGER
);
CREATE INDEX IF NOT EXISTS events_port ON events (port);
CREATE INDEX IF NOT EXISTS events_name ON events (name);

CREATE TABLE IF NOT EXISTS nodes (
    name       TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    pool_start INTEGER NOT NULL,
    pool_end   INTEGER NOT NULL,
    version    TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# Columns added after the first release, applied to databases that predate them.
ADDED_COLUMNS = {"ttl": "INTEGER"}

REGISTERED = "registered"
RENEWED = "renewed"
MOVED = "moved"
RELEASED = "released"
EXPIRED = "expired"

ACTIONS = (REGISTERED, RENEWED, MOVED, RELEASED, EXPIRED)

# Everything except a heartbeat keeping the port it already had. Sent to a chat
# channel, that one event is the reason people mute the channel.
NOTABLE = (REGISTERED, MOVED, RELEASED, EXPIRED)

# Enough to answer "what had this port last week" on a busy machine, and few
# enough that a warden left running for a year does not grow a database nobody
# asked for.
EVENT_CAP = 5000


def _isoformat(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _row_to_registration(row: sqlite3.Row) -> Registration:
    return Registration(
        name=row["name"],
        kind=row["kind"],
        project=row["project"],
        host=row["host"],
        port=row["port"],
        pid=row["pid"],
        meta=json.loads(row["meta"]),
        ttl=row["ttl"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        at=datetime.fromisoformat(row["at"]),
        action=row["action"],
        name=row["name"],
        kind=row["kind"],
        project=row["project"],
        host=row["host"],
        port=row["port"],
        pid=row["pid"],
    )


def _row_to_node(row: sqlite3.Row) -> Node:
    return Node(
        name=row["name"],
        url=row["url"],
        pool_start=row["pool_start"],
        pool_end=row["pool_end"],
        version=row["version"],
        first_seen=datetime.fromisoformat(row["first_seen"]),
        last_seen=datetime.fromisoformat(row["last_seen"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )


class Store:
    """SQLite-backed registry. Safe to share across threads."""

    def __init__(self, path: Path | str, *, event_cap: int = EVENT_CAP) -> None:
        self.path = path if path == ":memory:" else Path(path)
        self.event_cap = event_cap
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Event], None]] = []
        self._pending: list[Event] = []
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._add_missing_columns()
            self._db.commit()

    def _add_missing_columns(self) -> None:
        """Bring a database written by an older version up to the current schema."""
        present = {row["name"] for row in self._db.execute("PRAGMA table_info(registrations)")}
        for column, definition in ADDED_COLUMNS.items():
            if column not in present:
                self._db.execute(f"ALTER TABLE registrations ADD COLUMN {column} {definition}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def list(self, *, project: str | None = None, kind: str | None = None) -> list[Registration]:
        query = "SELECT * FROM registrations"
        clauses: list[str] = []
        params: list[object] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY port"
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [_row_to_registration(row) for row in rows]

    def get(self, name: str) -> Registration | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM registrations WHERE name = ?", (name,)
            ).fetchone()
        return _row_to_registration(row) if row else None

    def owner_of(self, host: str, port: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT name FROM registrations WHERE host = ? AND port = ?", (host, port)
            ).fetchone()
        return row["name"] if row else None

    def ports_on(self, host: str) -> set[int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT port FROM registrations WHERE host = ?", (host,)
            ).fetchall()
        return {row["port"] for row in rows}

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) AS n FROM registrations").fetchone()["n"]

    def _record(self, action: str, row: Registration | sqlite3.Row, at: datetime) -> None:
        """Write down what just happened, then drop the oldest if there are too many.

        Called with the lock already held and committed by the caller, so an
        event can never outlive the change it describes.
        """
        held = row if isinstance(row, sqlite3.Row) else row.model_dump()
        self._db.execute(
            """
            INSERT INTO events (at, action, name, kind, project, host, port, pid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _isoformat(at),
                action,
                held["name"],
                held["kind"],
                held["project"],
                held["host"],
                held["port"],
                held["pid"],
            ),
        )
        self._db.execute(
            "DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?",
            (self.event_cap,),
        )
        self._pending.append(
            Event(
                at=at,
                action=action,
                name=held["name"],
                kind=held["kind"],
                project=held["project"],
                host=held["host"],
                port=held["port"],
                pid=held["pid"],
            )
        )

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        """Hear about every change, once it is committed and not before."""
        self._listeners.append(listener)

    def _announce(self) -> None:
        """Hand out the events of the change that just landed.

        Called with the lock released, so a listener that takes its time cannot
        stand between the next caller and a port. A listener that raises is its
        own problem and never the writer's.
        """
        with self._lock:
            pending, self._pending = self._pending, []
        for event in pending:
            for listener in self._listeners:
                try:
                    listener(event)
                except Exception:
                    logger.exception("a listener failed on %s %s", event.action, event.name)

    def history(
        self, *, port: int | None = None, name: str | None = None, limit: int = 100
    ) -> list[Event]:
        """What happened to a port, to a service, or lately to anything."""
        query = "SELECT * FROM events"
        clauses: list[str] = []
        params: list[object] = []
        if port is not None:
            clauses.append("port = ?")
            params.append(port)
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def save(self, registration: Registration) -> None:
        with self._lock:
            previous = self._db.execute(
                "SELECT port FROM registrations WHERE name = ?", (registration.name,)
            ).fetchone()
            self._db.execute(
                """
                INSERT INTO registrations
                    (name, kind, project, host, port, pid, meta, ttl,
                     created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    kind = excluded.kind,
                    project = excluded.project,
                    host = excluded.host,
                    port = excluded.port,
                    pid = excluded.pid,
                    meta = excluded.meta,
                    ttl = excluded.ttl,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    registration.name,
                    registration.kind,
                    registration.project,
                    registration.host,
                    registration.port,
                    registration.pid,
                    json.dumps(registration.meta, separators=(",", ":")),
                    registration.ttl,
                    _isoformat(registration.created_at),
                    _isoformat(registration.updated_at),
                    _isoformat(registration.expires_at),
                ),
            )
            if previous is None:
                action = REGISTERED
            elif previous["port"] != registration.port:
                action = MOVED
            else:
                action = RENEWED
            self._record(action, registration, registration.updated_at)
            self._db.commit()
        self._announce()

    def delete(self, name: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM registrations WHERE name = ?", (name,)
            ).fetchone()
            cursor = self._db.execute("DELETE FROM registrations WHERE name = ?", (name,))
            if row is not None:
                self._record(RELEASED, row, datetime.now(UTC))
            self._db.commit()
        self._announce()
        return cursor.rowcount > 0

    def list_nodes(self) -> list[Node]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM nodes ORDER BY name").fetchall()
        return [_row_to_node(row) for row in rows]

    def get_node(self, name: str) -> Node | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM nodes WHERE name = ?", (name,)).fetchone()
        return _row_to_node(row) if row else None

    def count_nodes(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()["n"]

    def save_node(self, node: Node) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO nodes
                    (name, url, pool_start, pool_end, version,
                     first_seen, last_seen, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    url = excluded.url,
                    pool_start = excluded.pool_start,
                    pool_end = excluded.pool_end,
                    version = excluded.version,
                    last_seen = excluded.last_seen,
                    expires_at = excluded.expires_at
                """,
                (
                    node.name,
                    node.url,
                    node.pool_start,
                    node.pool_end,
                    node.version,
                    _isoformat(node.first_seen),
                    _isoformat(node.last_seen),
                    _isoformat(node.expires_at),
                ),
            )
            self._db.commit()

    def delete_node(self, name: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM nodes WHERE name = ?", (name,))
            self._db.commit()
        return cursor.rowcount > 0

    def purge_expired(self, now: datetime) -> list[str]:
        cutoff = _isoformat(now)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM registrations WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (cutoff,),
            ).fetchall()
            if rows:
                self._db.execute(
                    "DELETE FROM registrations "
                    "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (cutoff,),
                )
                for row in rows:
                    self._record(EXPIRED, row, now)
                self._db.commit()
        self._announce()
        return [row["name"] for row in rows]

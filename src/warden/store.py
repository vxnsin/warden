from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from warden.firewall.model import Rule
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

CREATE TABLE IF NOT EXISTS rules (
    name        TEXT PRIMARY KEY,
    direction   TEXT NOT NULL,
    action      TEXT NOT NULL,
    protocol    TEXT NOT NULL,
    ports       TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL,
    destination TEXT NOT NULL,
    interface   TEXT,
    origin      TEXT NOT NULL,
    service     TEXT,
    expires_at  TEXT,
    comment     TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rules_origin ON rules (origin);
CREATE INDEX IF NOT EXISTS rules_service ON rules (service);

CREATE TABLE IF NOT EXISTS snapshots (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    backend TEXT NOT NULL,
    body    TEXT NOT NULL,
    reason  TEXT
);

-- One row or none: either a rollback is armed or it is not.
CREATE TABLE IF NOT EXISTS pending (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot INTEGER NOT NULL,
    deadline TEXT NOT NULL,
    reason   TEXT
);

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
        self.save_many([registration])

    def save_many(self, registrations: list[Registration]) -> None:
        """Write them all, or write none of them.

        One transaction, because ports asked for together must never end up
        half held - a caller told it has four ports and given three has no way
        of knowing which promise to believe.
        """
        with self._lock:
            try:
                for registration in registrations:
                    self._write(registration)
            except Exception:
                self._db.rollback()
                self._pending.clear()
                raise
            self._db.commit()
        self._announce()

    def _write(self, registration: Registration) -> None:
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


def _row_to_rule(row: sqlite3.Row) -> Rule:
    return Rule(
        name=row["name"],
        direction=row["direction"],
        action=row["action"],
        protocol=row["protocol"],
        ports=set(json.loads(row["ports"])),
        source=row["source"],
        destination=row["destination"],
        interface=row["interface"],
        origin=row["origin"],
        service=row["service"],
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        comment=row["comment"],
        enabled=bool(row["enabled"]),
    )


class RuleStore:
    """The firewall's rules, in the same database and under the same lock.

    Separate class, one connection: two stores over one SQLite file would take
    turns waiting for each other, and a rule and the registration it borrowed
    its lease from should be able to change together.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def list(self, *, origin: str | None = None) -> list[Rule]:
        query = "SELECT * FROM rules"
        params: list[object] = []
        if origin:
            query += " WHERE origin = ?"
            params.append(origin)
        query += " ORDER BY created_at, name"
        with self._store._lock:
            rows = self._store._db.execute(query, params).fetchall()
        return [_row_to_rule(row) for row in rows]

    def get(self, name: str) -> Rule | None:
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT * FROM rules WHERE name = ?", (name,)
            ).fetchone()
        return _row_to_rule(row) if row else None

    def save(self, rule: Rule) -> None:
        self.save_many([rule])

    def save_many(self, rules: list[Rule]) -> None:
        """All of them or none, like everything else that writes here."""
        now = _isoformat(datetime.now(UTC))
        with self._store._lock:
            try:
                for rule in rules:
                    self._store._db.execute(
                        """
                        INSERT INTO rules
                            (name, direction, action, protocol, ports, source,
                             destination, interface, origin, service, expires_at,
                             comment, enabled, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            direction = excluded.direction,
                            action = excluded.action,
                            protocol = excluded.protocol,
                            ports = excluded.ports,
                            source = excluded.source,
                            destination = excluded.destination,
                            interface = excluded.interface,
                            origin = excluded.origin,
                            service = excluded.service,
                            expires_at = excluded.expires_at,
                            comment = excluded.comment,
                            enabled = excluded.enabled
                        """,
                        (
                            rule.name,
                            rule.direction,
                            rule.action,
                            rule.protocol,
                            json.dumps(sorted(rule.ports)),
                            rule.source,
                            rule.destination,
                            rule.interface,
                            rule.origin,
                            rule.service,
                            _isoformat(rule.expires_at),
                            rule.comment,
                            int(rule.enabled),
                            now,
                        ),
                    )
            except Exception:
                self._store._db.rollback()
                raise
            self._store._db.commit()

    def delete(self, name: str) -> bool:
        with self._store._lock:
            cursor = self._store._db.execute("DELETE FROM rules WHERE name = ?", (name,))
            self._store._db.commit()
        return cursor.rowcount > 0

    def delete_many(self, names: list[str]) -> int:
        if not names:
            return 0
        marks = ", ".join("?" for _ in names)
        with self._store._lock:
            cursor = self._store._db.execute(
                f"DELETE FROM rules WHERE name IN ({marks})", names
            )
            self._store._db.commit()
        return cursor.rowcount


class Snapshots:
    """What the firewall looked like before, and whether it is going back.

    Kept in the database rather than a file so the watchdog, the command that
    armed it and the one that confirms it are all looking at the same thing,
    whichever of them is still alive.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def take(self, backend: str, body: str, reason: str | None = None) -> int:
        with self._store._lock:
            cursor = self._store._db.execute(
                "INSERT INTO snapshots (at, backend, body, reason) VALUES (?, ?, ?, ?)",
                (_isoformat(datetime.now(UTC)), backend, body, reason),
            )
            self._store._db.commit()
        return int(cursor.lastrowid or 0)

    def body(self, snapshot: int) -> str | None:
        with self._store._lock:
            row = self._store._db.execute(
                "SELECT body FROM snapshots WHERE id = ?", (snapshot,)
            ).fetchone()
        return row["body"] if row else None

    def latest(self) -> sqlite3.Row | None:
        with self._store._lock:
            return self._store._db.execute(
                "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def arm(self, snapshot: int, deadline: datetime, reason: str | None = None) -> None:
        """Say that the firewall goes back at this moment unless told otherwise."""
        with self._store._lock:
            self._store._db.execute(
                """
                INSERT INTO pending (id, snapshot, deadline, reason) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    snapshot = excluded.snapshot,
                    deadline = excluded.deadline,
                    reason = excluded.reason
                """,
                (snapshot, _isoformat(deadline), reason),
            )
            self._store._db.commit()

    def armed(self) -> tuple[int, datetime, str | None] | None:
        with self._store._lock:
            row = self._store._db.execute("SELECT * FROM pending WHERE id = 1").fetchone()
        if row is None:
            return None
        return row["snapshot"], datetime.fromisoformat(row["deadline"]), row["reason"]

    def disarm(self) -> bool:
        """Called by confirming, and by the rollback once it has happened."""
        with self._store._lock:
            cursor = self._store._db.execute("DELETE FROM pending WHERE id = 1")
            self._store._db.commit()
        return cursor.rowcount > 0

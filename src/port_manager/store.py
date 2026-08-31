from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from port_manager.models import Registration

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
"""

# Columns added after the first release, applied to databases that predate them.
ADDED_COLUMNS = {"ttl": "INTEGER"}


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


class Store:
    """SQLite-backed registry. Safe to share across threads."""

    def __init__(self, path: Path | str) -> None:
        self.path = path if path == ":memory:" else Path(path)
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
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

    def save(self, registration: Registration) -> None:
        with self._lock:
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
            self._db.commit()

    def delete(self, name: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM registrations WHERE name = ?", (name,))
            self._db.commit()
        return cursor.rowcount > 0

    def purge_expired(self, now: datetime) -> list[str]:
        cutoff = _isoformat(now)
        with self._lock:
            rows = self._db.execute(
                "SELECT name FROM registrations WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (cutoff,),
            ).fetchall()
            if rows:
                self._db.execute(
                    "DELETE FROM registrations "
                    "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (cutoff,),
                )
                self._db.commit()
        return [row["name"] for row in rows]

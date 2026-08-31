import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from warden.models import Node, Registration
from warden.store import Store


def registration(name: str, port: int, **kwargs) -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind=kwargs.pop("kind", "backend"),
        project=kwargs.pop("project", None),
        host=kwargs.pop("host", "127.0.0.1"),
        port=port,
        pid=kwargs.pop("pid", None),
        meta=kwargs.pop("meta", {}),
        ttl=kwargs.pop("ttl", None),
        created_at=now,
        updated_at=now,
        expires_at=kwargs.pop("expires_at", None),
    )


def test_a_saved_registration_comes_back_unchanged(store: Store):
    saved = registration("api", 8000, project="shop", meta={"branch": "main"})
    store.save(saved)
    assert store.get("api") == saved


def test_an_unknown_name_returns_nothing(store: Store):
    assert store.get("api") is None


def test_two_services_cannot_share_an_endpoint(store: Store):
    store.save(registration("api", 8000))
    with pytest.raises(sqlite3.IntegrityError):
        store.save(registration("web", 8000))


def test_the_same_port_is_allowed_on_another_host(store: Store):
    store.save(registration("api", 8000))
    store.save(registration("remote", 8000, host="10.0.0.5"))
    assert store.ports_on("127.0.0.1") == {8000}


def test_listing_filters_by_project_and_kind(store: Store):
    store.save(registration("api", 8000, project="shop"))
    store.save(registration("web", 8001, project="shop", kind="frontend"))
    store.save(registration("other", 8002, project="blog"))
    assert [r.name for r in store.list(project="shop")] == ["api", "web"]
    assert [r.name for r in store.list(kind="frontend")] == ["web"]


def test_listing_is_ordered_by_port(store: Store):
    store.save(registration("web", 8002))
    store.save(registration("api", 8001))
    assert [r.port for r in store.list()] == [8001, 8002]


def test_expired_registrations_are_purged(store: Store):
    now = datetime.now(UTC)
    store.save(registration("gone", 8000, expires_at=now - timedelta(seconds=1)))
    store.save(registration("stays", 8001, expires_at=now + timedelta(hours=1)))
    store.save(registration("forever", 8002))
    assert store.purge_expired(now) == ["gone"]
    assert [r.name for r in store.list()] == ["stays", "forever"]


def test_deleting_reports_whether_anything_was_removed(store: Store):
    store.save(registration("api", 8000))
    assert store.delete("api") is True
    assert store.delete("api") is False


OLD_SCHEMA = """
CREATE TABLE registrations (
    name       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    project    TEXT,
    host       TEXT NOT NULL,
    port       INTEGER NOT NULL,
    pid        INTEGER,
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);
"""


def test_a_database_from_an_older_version_keeps_its_registrations(tmp_path):
    path = tmp_path / "old.db"
    now = datetime.now(UTC).isoformat()
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.execute(
        "INSERT INTO registrations (name, kind, host, port, meta, created_at, updated_at) "
        "VALUES ('api', 'backend', '127.0.0.1', 8000, '{}', ?, ?)",
        (now, now),
    )
    old.commit()
    old.close()

    with Store(path) as store:
        registration = store.get("api")
        assert registration is not None
        assert registration.port == 8000
        assert registration.ttl is None


def node(name: str = "build-01", **kwargs) -> Node:
    now = datetime.now(UTC)
    return Node(
        name=name,
        url=kwargs.pop("url", f"http://{name}:7010"),
        pool_start=kwargs.pop("pool_start", 9000),
        pool_end=kwargs.pop("pool_end", 9099),
        version=kwargs.pop("version", "0.1.0"),
        first_seen=kwargs.pop("first_seen", now),
        last_seen=kwargs.pop("last_seen", now),
        expires_at=kwargs.pop("expires_at", now + timedelta(seconds=90)),
    )


def test_a_saved_node_comes_back_unchanged(store: Store):
    saved = node()
    store.save_node(saved)
    assert store.get_node("build-01") == saved


def test_an_unknown_node_returns_nothing(store: Store):
    assert store.get_node("build-01") is None


def test_saving_a_node_twice_updates_rather_than_duplicates(store: Store):
    store.save_node(node())
    store.save_node(node(url="http://10.0.0.7:7010"))
    assert store.count_nodes() == 1
    assert store.get_node("build-01").url == "http://10.0.0.7:7010"


def test_the_first_sighting_of_a_node_is_not_overwritten(store: Store):
    first = datetime.now(UTC) - timedelta(days=3)
    store.save_node(node(first_seen=first))
    store.save_node(node(first_seen=datetime.now(UTC)))
    assert store.get_node("build-01").first_seen == first


def test_nodes_are_listed_by_name(store: Store):
    for name in ("web-02", "build-01"):
        store.save_node(node(name))
    assert [row.name for row in store.list_nodes()] == ["build-01", "web-02"]


def test_deleting_a_node_reports_whether_anything_went(store: Store):
    store.save_node(node())
    assert store.delete_node("build-01") is True
    assert store.delete_node("build-01") is False


def test_a_database_from_before_the_fleet_gains_the_table(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.commit()
    old.close()

    with Store(path) as store:
        store.save_node(node())
        assert store.count_nodes() == 1

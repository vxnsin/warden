from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from warden import __version__
from warden.api import create_app
from warden.config import Settings
from warden.metrics import CONTENT_TYPE, render
from warden.models import Node, PoolStatus, Registration


def pool(**overrides) -> PoolStatus:
    values = {
        "start": 8000,
        "end": 8999,
        "size": 1000,
        "reserved": [8080],
        "allocated": 2,
        "available": 997,
    }
    values.update(overrides)
    return PoolStatus(**values)


def service(name: str, kind: str) -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind=kind,
        project=None,
        host="127.0.0.1",
        port=8000,
        pid=None,
        meta={},
        ttl=None,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )


def node(name: str, *, online: bool = True) -> Node:
    now = datetime.now(UTC)
    return Node(
        name=name,
        url=f"http://{name}:7010",
        pool_start=8000,
        pool_end=8999,
        version=__version__,
        first_seen=now,
        last_seen=now,
        expires_at=now + timedelta(seconds=90) if online else now - timedelta(seconds=1),
    )


def scrape(**overrides) -> str:
    values = {
        "pool": pool(),
        "services": [],
        "nodes": [],
        "version": __version__,
        "node": "hub",
        "role": "hub",
    }
    values.update(overrides)
    return render(**values)


def lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith("#")]


def test_every_metric_says_what_it_is_and_what_kind_it_is():
    text = scrape()
    names = {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE")}
    for line in lines(text):
        assert line.split("{")[0].split(" ")[0] in names


def test_the_version_node_and_role_come_through():
    assert (
        f'warden_info{{version="{__version__}",node="hub",role="hub"}} 1' in scrape()
    )


def test_the_pool_numbers_are_the_ones_the_pool_reports():
    text = scrape()
    assert "warden_pool_ports 1000" in text
    assert "warden_pool_allocated 2" in text
    assert "warden_pool_available 997" in text
    assert "warden_pool_reserved 1" in text


def test_services_are_counted_by_kind():
    text = scrape(
        services=[service("api", "backend"), service("web", "frontend"), service("q", "backend")]
    )
    assert 'warden_services{kind="backend"} 2' in text
    assert 'warden_services{kind="frontend"} 1' in text


def test_nothing_registered_is_a_zero_rather_than_a_missing_metric():
    # A metric that disappears looks like a broken scrape, not an empty warden.
    assert "warden_services 0" in scrape()
    assert "warden_nodes 0" in scrape()


def test_nodes_are_counted_by_status():
    text = scrape(nodes=[node("build-01"), node("build-02"), node("old", online=False)])
    assert 'warden_nodes{status="online"} 2' in text
    assert 'warden_nodes{status="stale"} 1' in text


def test_a_label_that_could_break_the_format_is_escaped():
    text = scrape(node='he said "no"')
    assert r'node="he said \"no\""' in text


def test_the_body_ends_with_a_newline():
    # Without it the last sample is dropped by some scrapers.
    assert scrape().endswith("\n")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def test_the_endpoint_serves_the_prometheus_content_type(client: TestClient):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE


def test_the_endpoint_counts_what_is_registered(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    body = client.get("/metrics").text
    assert 'warden_services{kind="backend"} 1' in body
    assert "warden_pool_allocated 1" in body


def test_metrics_are_behind_the_same_token_as_every_other_read(tmp_path):
    # Left open on a warden bound to 0.0.0.0 this hands out the shape of the
    # whole fleet to anyone who asks.
    guarded = Settings(
        database=tmp_path / "registry.db", token="secret", probe=False, update_check=False
    )
    with TestClient(create_app(guarded)) as client:
        assert client.get("/metrics").status_code == 401
        allowed = client.get("/metrics", headers={"Authorization": "Bearer secret"})
        assert allowed.status_code == 200


def test_the_cluster_token_can_scrape_too(tmp_path):
    guarded = Settings(
        database=tmp_path / "registry.db",
        token="secret",
        cluster_token="fleet",
        probe=False,
        update_check=False,
    )
    with TestClient(create_app(guarded)) as client:
        response = client.get("/metrics", headers={"Authorization": "Bearer fleet"})
        assert response.status_code == 200

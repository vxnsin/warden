from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from port_manager.api import create_app
from port_manager.config import Settings


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def test_health_reports_the_number_of_services(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["services"] == 1


def test_registering_returns_a_port(client: TestClient):
    response = client.post("/v1/services", json={"name": "api", "kind": "backend"})
    assert response.status_code == 201
    assert response.json()["port"] == 8000


def test_registering_again_renews_instead_of_creating(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    response = client.post("/v1/services", json={"name": "api", "kind": "backend", "pid": 99})
    assert response.status_code == 200
    assert response.json()["port"] == 8000
    assert response.json()["pid"] == 99


def test_a_service_can_look_up_a_neighbour(client: TestClient):
    client.post(
        "/v1/services", json={"name": "api", "kind": "backend", "project": "shop"}
    )
    response = client.get("/v1/services/api")
    assert response.status_code == 200
    assert response.json()["port"] == 8000


def test_listing_can_be_filtered_by_project(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend", "project": "shop"})
    client.post("/v1/services", json={"name": "blog", "kind": "backend", "project": "blog"})
    names = [item["name"] for item in client.get("/v1/services?project=shop").json()]
    assert names == ["api"]


def test_an_unknown_service_is_a_404(client: TestClient):
    response = client.get("/v1/services/nothing")
    assert response.status_code == 404
    assert "nothing" in response.json()["detail"]


def test_a_taken_port_is_a_409(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    response = client.post(
        "/v1/services", json={"name": "web", "kind": "frontend", "preferred_port": 8000}
    )
    assert response.status_code == 409


def test_an_exhausted_pool_is_a_503(client: TestClient):
    for index in range(5):
        client.post("/v1/services", json={"name": f"service-{index}", "kind": "worker"})
    response = client.post("/v1/services", json={"name": "late", "kind": "worker"})
    assert response.status_code == 503


def test_releasing_frees_the_port(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    assert client.delete("/v1/services/api").status_code == 204
    assert client.delete("/v1/services/api").status_code == 404


def test_a_heartbeat_extends_the_lease(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend", "ttl": 30})
    response = client.post("/v1/services/api/heartbeat", json={"ttl": 60})
    assert response.status_code == 200
    assert response.json()["expires_at"] is not None


def test_pool_usage_is_reported(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    body = client.get("/v1/pool").json()
    assert (body["size"], body["allocated"], body["available"]) == (5, 1, 4)


def test_an_invalid_name_is_rejected(client: TestClient):
    response = client.post("/v1/services", json={"name": "Not A Name", "kind": "backend"})
    assert response.status_code == 422


def test_unknown_fields_are_rejected(client: TestClient):
    response = client.post(
        "/v1/services", json={"name": "api", "kind": "backend", "prot": 8000}
    )
    assert response.status_code == 422


def test_a_token_protects_the_registry(settings: Settings):
    with TestClient(create_app(settings.model_copy(update={"token": "secret"}))) as client:
        assert client.get("/v1/services").status_code == 401
        authorized = client.get("/v1/services", headers={"Authorization": "Bearer secret"})
        assert authorized.status_code == 200
        assert client.get("/health").status_code == 200

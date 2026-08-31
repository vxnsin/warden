from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from warden.api import create_app
from warden.config import Settings


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
        "/v1/services", json={"name": "web", "kind": "frontend", "require_port": 8000}
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


def test_a_wished_for_port_falls_back_when_it_is_taken(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    response = client.post(
        "/v1/services", json={"name": "web", "kind": "frontend", "preferred_port": 8000}
    )
    assert response.status_code == 201
    assert response.json()["port"] == 8001


def test_a_wish_and_a_demand_together_are_rejected(client: TestClient):
    response = client.post(
        "/v1/services",
        json={"name": "api", "kind": "backend", "preferred_port": 8000, "require_port": 8001},
    )
    assert response.status_code == 422


def test_the_listeners_of_this_machine_are_reported(client: TestClient):
    response = client.get("/v1/listeners")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_stopping_a_process_over_the_api_is_off_by_default(client: TestClient):
    response = client.delete("/v1/listeners/999999")
    assert response.status_code == 403
    assert "WARDEN_ALLOW_KILL" in response.json()["detail"]


def test_the_switch_opens_the_door_but_the_process_must_exist(settings: Settings):
    permitted = settings.model_copy(update={"allow_kill": True})
    with TestClient(create_app(permitted)) as client:
        response = client.delete("/v1/listeners/999999")
        assert response.status_code == 404


NODE = {
    "name": "build-01",
    "url": "http://build-01:7010",
    "pool_start": 9000,
    "pool_end": 9099,
    "version": "0.1.0",
}


def test_a_node_can_announce_itself(client: TestClient):
    response = client.post("/v1/nodes", json=NODE)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "build-01"
    assert body["status"] == "online"


def test_announcing_again_renews_the_node(client: TestClient):
    client.post("/v1/nodes", json=NODE)
    assert client.post("/v1/nodes", json=NODE).status_code == 200
    assert len(client.get("/v1/nodes").json()) == 1


def test_the_known_nodes_are_listed(client: TestClient):
    client.post("/v1/nodes", json=NODE)
    client.post("/v1/nodes", json={**NODE, "name": "web-02", "url": "http://web-02:7010"})
    assert [row["name"] for row in client.get("/v1/nodes").json()] == ["build-01", "web-02"]


def test_a_node_can_be_forgotten(client: TestClient):
    client.post("/v1/nodes", json=NODE)
    assert client.delete("/v1/nodes/build-01").status_code == 204
    assert client.delete("/v1/nodes/build-01").status_code == 404


def test_an_address_that_is_not_a_url_is_rejected(client: TestClient):
    assert client.post("/v1/nodes", json={**NODE, "url": "build-01:7010"}).status_code == 422


def test_health_says_which_warden_this_is(client: TestClient):
    body = client.get("/health").json()
    assert body["role"] == "hub"
    assert body["nodes"] == 0
    assert body["node"]


def test_health_counts_the_nodes_it_knows(client: TestClient):
    client.post("/v1/nodes", json=NODE)
    assert client.get("/health").json()["nodes"] == 1


def test_announcing_needs_the_cluster_token_not_the_api_one(settings: Settings):
    guarded = settings.model_copy(update={"token": "human", "cluster_token": "between-wardens"})
    with TestClient(create_app(guarded)) as client:
        assert client.post("/v1/nodes", json=NODE).status_code == 401
        human = {"Authorization": "Bearer human"}
        assert client.post("/v1/nodes", json=NODE, headers=human).status_code == 401
        cluster = {"Authorization": "Bearer between-wardens"}
        assert client.post("/v1/nodes", json=NODE, headers=cluster).status_code == 201
        # Reading the fleet is for people, so it takes the human token.
        assert client.get("/v1/nodes", headers=cluster).status_code == 401
        assert client.get("/v1/nodes", headers=human).status_code == 200

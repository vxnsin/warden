from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from warden import aggregate
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


@pytest.mark.sockets
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


HUMAN = {"Authorization": "Bearer human"}
CLUSTER = {"Authorization": "Bearer between-wardens"}


def guarded(settings: Settings) -> TestClient:
    return TestClient(
        create_app(
            settings.model_copy(
                update={"token": "human", "cluster_token": "between-wardens"}
            )
        )
    )


def test_announcing_takes_the_cluster_token_and_not_a_persons(settings: Settings):
    with guarded(settings) as client:
        assert client.post("/v1/nodes", json=NODE).status_code == 401
        assert client.post("/v1/nodes", json=NODE, headers=HUMAN).status_code == 401
        assert client.post("/v1/nodes", json=NODE, headers=CLUSTER).status_code == 201


def test_either_token_may_read(settings: Settings):
    # A hub fanning out to its nodes carries the cluster token; a person carries
    # theirs. Both are only reading.
    with guarded(settings) as client:
        for path in (
            "/v1/services",
            "/v1/pool",
            "/v1/nodes",
            "/v1/fleet/services",
            "/v1/fleet/pool",
        ):
            assert client.get(path).status_code == 401, path
            assert client.get(path, headers=HUMAN).status_code == 200, path
            assert client.get(path, headers=CLUSTER).status_code == 200, path


def test_the_cluster_token_changes_nothing(settings: Settings):
    with guarded(settings) as client:
        client.post("/v1/nodes", json=NODE, headers=CLUSTER)
        assert client.delete("/v1/nodes/build-01", headers=CLUSTER).status_code == 401
        assert client.post(
            "/v1/services", json={"name": "api", "kind": "backend"}, headers=CLUSTER
        ).status_code == 401
        assert client.delete("/v1/nodes/build-01", headers=HUMAN).status_code == 204


def test_the_fleet_view_includes_the_hubs_own_services(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    body = client.get("/v1/fleet/services").json()
    assert [(s["node"], s["name"]) for s in body["services"]] == [("hub", "api")]
    assert body["unreachable"] == []


def test_a_node_that_cannot_be_reached_is_named_in_the_fleet_view(client: TestClient):
    client.post("/v1/nodes", json={**NODE, "url": "http://192.0.2.1:7010"})
    body = client.get("/v1/fleet/services").json()
    assert [u["node"] for u in body["unreachable"]] == ["build-01"]


def test_the_fleet_pool_includes_the_hubs_own(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    body = client.get("/v1/fleet/pool").json()
    assert [(p["node"], p["allocated"]) for p in body["pools"]] == [("hub", 1)]
    assert body["allocated"] == 1
    assert body["capacity"] == 5


def test_a_node_that_cannot_be_reached_is_named_in_the_fleet_pool(client: TestClient):
    client.post("/v1/nodes", json={**NODE, "url": "http://192.0.2.1:7010"})
    body = client.get("/v1/fleet/pool").json()
    assert [u["node"] for u in body["unreachable"]] == ["build-01"]
    # The hub still answers for itself; one dead node is not a dead listing.
    assert [p["node"] for p in body["pools"]] == ["hub"]


@pytest.mark.sockets
def test_the_fleet_ports_include_this_machines_own(client: TestClient):
    body = client.get("/v1/fleet/listeners").json()
    assert {item["node"] for item in body["listeners"]} <= {"hub"}
    assert body["unreachable"] == []


def test_stopping_a_process_on_this_warden_still_needs_it_switched_on(client: TestClient):
    response = client.delete("/v1/fleet/listeners/hub/99")
    assert response.status_code == 403
    assert "WARDEN_ALLOW_KILL" in response.json()["detail"]


def test_a_qualified_lookup_on_this_warden_itself(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    response = client.get("/v1/fleet/services/hub/api")
    assert response.status_code == 200
    assert response.json()["node"] == "hub"


def test_a_qualified_lookup_on_a_node_nobody_knows(client: TestClient):
    assert client.get("/v1/fleet/services/nowhere/api").status_code == 404


def test_registering_through_the_hub_on_the_hub_itself(client: TestClient):
    response = client.post("/v1/fleet/services/hub", json={"name": "api", "kind": "backend"})
    assert response.status_code == 201
    assert response.json() == {**response.json(), "node": "hub", "port": 8000}


def test_registering_again_through_the_hub_renews(client: TestClient):
    client.post("/v1/fleet/services/hub", json={"name": "api", "kind": "backend"})
    response = client.post("/v1/fleet/services/hub", json={"name": "api", "kind": "backend"})
    assert response.status_code == 200


def test_a_heartbeat_through_the_hub_on_the_hub_itself(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend", "ttl": 60})
    response = client.post("/v1/fleet/services/hub/api/heartbeat", json={})
    assert response.status_code == 200
    assert response.json()["node"] == "hub"
    assert response.json()["expires_at"] is not None


def test_releasing_through_the_hub_on_the_hub_itself(client: TestClient):
    client.post("/v1/services", json={"name": "api", "kind": "backend"})
    assert client.delete("/v1/fleet/services/hub/api").status_code == 204
    assert client.get("/v1/services/api").status_code == 404


def test_registering_on_a_node_nobody_knows(client: TestClient):
    response = client.post("/v1/fleet/services/nowhere", json={"name": "api", "kind": "backend"})
    assert response.status_code == 404
    assert "nowhere" in response.json()["detail"]


def test_a_node_that_cannot_be_reached_says_which_node(client: TestClient):
    client.post("/v1/nodes", json={**NODE, "url": "http://192.0.2.1:7010"})
    response = client.delete("/v1/fleet/services/build-01/api")
    assert response.status_code == 404
    assert "build-01" in response.json()["detail"]


def answering(handler, seen: list) -> object:
    """Stand in for the node the hub relays to, and record what it was asked.

    What the recorded authorization then does on the wire is `relaying`'s own
    business, and tested where that lives.
    """

    def relaying(authorization: str | None, timeout: float = aggregate.TIMEOUT):
        def respond(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), authorization))
            return handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    return relaying


def test_a_refusal_from_a_node_reaches_the_caller_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    client.post("/v1/nodes", json=NODE)
    seen: list = []
    monkeypatch.setattr(
        aggregate,
        "relaying",
        answering(
            lambda request: httpx.Response(409, json={"detail": "port 3000 is held by 'crm'"}),
            seen,
        ),
    )
    response = client.post(
        "/v1/fleet/services/build-01",
        json={"name": "api", "kind": "backend", "require_port": 3000},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "port 3000 is held by 'crm'"
    assert seen[0][0] == "http://build-01:7010/v1/services"


def test_the_hub_hands_on_the_callers_token_and_not_its_own(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    seen: list = []
    monkeypatch.setattr(
        aggregate,
        "relaying",
        answering(lambda request: httpx.Response(204), seen),
    )
    with guarded(settings) as client:
        client.post("/v1/nodes", json=NODE, headers=CLUSTER)
        assert client.delete(
            "/v1/fleet/services/build-01/api", headers=HUMAN
        ).status_code == 204
    assert seen[0][1] == HUMAN["Authorization"]


def test_forwarding_a_registration_takes_a_persons_token_not_the_cluster_one(
    settings: Settings,
):
    # Otherwise the hub would become the one door the cluster token can write
    # through, which is exactly what it is not for.
    with guarded(settings) as client:
        body = {"name": "api", "kind": "backend"}
        assert client.post("/v1/fleet/services/hub", json=body).status_code == 401
        assert client.post(
            "/v1/fleet/services/hub", json=body, headers=CLUSTER
        ).status_code == 401
        assert client.post(
            "/v1/fleet/services/hub", json=body, headers=HUMAN
        ).status_code == 201


def test_the_update_status_is_readable(client: TestClient):
    body = client.get("/v1/update").json()
    assert body["current"]
    assert body["available"] is False


def test_updating_over_the_api_is_off_by_default(client: TestClient):
    response = client.post("/v1/update")
    assert response.status_code == 403
    assert "WARDEN_ALLOW_REMOTE_UPDATE" in response.json()["detail"]


def test_a_warden_allowed_to_update_still_needs_to_know_how(settings: Settings):
    willing = settings.model_copy(update={"allow_remote_update": True})
    with TestClient(create_app(willing)) as client:
        response = client.post("/v1/update")
        assert response.status_code == 403
        assert "WARDEN_UPDATE_COMMAND" in response.json()["detail"]


def test_either_token_may_ask_a_warden_to_update_itself(settings: Settings):
    # The hub does this on its rounds with the cluster token; an operator does
    # it by hand with theirs. WARDEN_ALLOW_REMOTE_UPDATE is the real gate, and
    # 403 here means the token was accepted and the setting was not.
    with guarded(settings) as client:
        assert client.post("/v1/update").status_code == 401
        assert client.post("/v1/update", headers=HUMAN).status_code == 403
        assert client.post("/v1/update", headers=CLUSTER).status_code == 403


def test_a_fleet_update_reports_every_warden_including_this_one(client: TestClient):
    client.post("/v1/nodes", json={**NODE, "url": "http://192.0.2.1:7010"})
    body = client.post("/v1/fleet/update").json()
    assert {row["node"] for row in body["results"]} == {"hub", "build-01"}
    assert all(row["ok"] is False for row in body["results"])
    # The hub says why it refused itself, rather than pretending it worked.
    here = next(row for row in body["results"] if row["node"] == "hub")
    assert "WARDEN_ALLOW_REMOTE_UPDATE" in here["detail"]


def test_a_node_cannot_be_re_announced_somewhere_else(client: TestClient):
    client.post("/v1/nodes", json=NODE)
    response = client.post("/v1/nodes", json={**NODE, "url": "http://elsewhere:7010"})
    assert response.status_code == 409
    assert "--forget build-01" in response.json()["detail"]
    assert client.get("/v1/nodes").json()[0]["url"] == NODE["url"]


def test_requiring_https_keeps_a_plain_http_node_out(settings: Settings):
    strict = settings.model_copy(update={"require_https": True})
    with TestClient(create_app(strict)) as client:
        assert client.post("/v1/nodes", json=NODE).status_code == 403
        secure = {**NODE, "url": "https://build-01:7010"}
        assert client.post("/v1/nodes", json=secure).status_code == 201

"""Driving the firewall over the API, which is off until somebody says otherwise."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from warden.api import create_app
from warden.client import WardenClient
from warden.core import health
from warden.core.config import Settings
from warden.errors import NotPermittedError


def allowing(settings: Settings, **more: object) -> Settings:
    """A warden that may be asked about its firewall over the network."""
    return settings.model_copy(
        update={
            "allow_remote_firewall": True,
            "firewall_from_registry": True,
            "firewall_allow_from": {"10.0.0.0/8"},
            **more,
        }
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A warden with the defaults, which is to say with the switch off."""
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def open_to_asking(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(allowing(settings))) as client:
        yield client


def a_service(client: TestClient, name: str = "shop-api", **more: object) -> dict:
    body = {"name": name, "kind": "backend", "host": "10.4.0.7", **more}
    return client.post("/v1/services", json=body).json()


def test_the_status_is_readable_without_the_switch(client: TestClient):
    """Reading is what the token already allows. Changing is the thing that is gated."""
    response = client.get("/v1/firewall")
    assert response.status_code == 200
    assert response.json()["remote"] is False
    assert response.json()["rules"] == 0


def test_opening_a_port_is_refused_until_it_is_switched_on(client: TestClient):
    a_service(client)
    response = client.post("/v1/firewall/open", json={"service": "shop-api"})
    assert response.status_code == 403
    assert "allow_remote_firewall" in response.json()["detail"]


def test_applying_is_refused_until_it_is_switched_on(client: TestClient):
    assert client.post("/v1/firewall/apply").status_code == 403


def test_deleting_a_rule_is_refused_until_it_is_switched_on(client: TestClient):
    assert client.delete("/v1/firewall/rules/allow-shop-api").status_code == 403


def test_switched_on_a_service_can_be_let_through(open_to_asking: TestClient):
    a_service(open_to_asking)
    response = open_to_asking.post(
        "/v1/firewall/open", json={"service": "shop-api", "source": "10.0.0.0/8"}
    )
    assert response.status_code == 200
    rule = response.json()
    assert rule["ports"] == [8000]
    assert rule["origin"] == "registry"
    assert rule["service"] == "shop-api"
    assert rule["source"] == "10.0.0.0/8"


def test_one_declared_network_does_not_have_to_be_named(open_to_asking: TestClient):
    a_service(open_to_asking)
    response = open_to_asking.post("/v1/firewall/open", json={"service": "shop-api"})
    assert response.json()["source"] == "10.0.0.0/8"


def test_two_declared_networks_have_to_be_chosen_between(settings: Settings):
    """Picking one for a caller who did not pick is warden making the decision."""
    more = allowing(settings, firewall_allow_from={"10.0.0.0/8", "192.168.0.0/16"})
    with TestClient(create_app(more)) as client:
        a_service(client)
        response = client.post("/v1/firewall/open", json={"service": "shop-api"})
        assert response.status_code == 403
        assert "name the network" in response.json()["detail"]


def test_a_source_outside_what_is_declared_is_refused(open_to_asking: TestClient):
    """The switch says who may ask. The bounds still say what may be asked for."""
    a_service(open_to_asking)
    response = open_to_asking.post(
        "/v1/firewall/open", json={"service": "shop-api", "source": "203.0.113.0/24"}
    )
    assert response.status_code == 403
    assert "10.0.0.0/8" in response.json()["detail"]


def test_a_service_on_loopback_has_nothing_to_open(open_to_asking: TestClient):
    a_service(open_to_asking, host="127.0.0.1")
    response = open_to_asking.post("/v1/firewall/open", json={"service": "shop-api"})
    assert response.status_code == 403
    assert "nothing to open" in response.json()["detail"]


def test_a_service_nobody_registered_is_a_404(open_to_asking: TestClient):
    response = open_to_asking.post("/v1/firewall/open", json={"service": "ghost"})
    assert response.status_code == 404


def test_a_port_outside_the_pool_never_becomes_a_rule(settings: Settings):
    """22 is outside the pool, so nothing can hold it and nothing can open it."""
    narrow = allowing(settings, pool_start=8000, pool_end=8004)
    with TestClient(create_app(narrow)) as client:
        taken = client.post("/v1/services", json={"name": "sshd", "kind": "backend", "port": 22})
        assert taken.status_code >= 400
        assert client.post("/v1/firewall/open", json={"service": "sshd"}).status_code == 404


def test_what_was_opened_shows_up_in_the_rules_and_the_count(open_to_asking: TestClient):
    a_service(open_to_asking)
    open_to_asking.post("/v1/firewall/open", json={"service": "shop-api"})
    rules = open_to_asking.get("/v1/firewall/rules").json()
    assert [rule["name"] for rule in rules] == ["allow-shop-api"]
    assert open_to_asking.get("/v1/firewall").json()["from_registry"] == 1
    assert open_to_asking.get("/v1/firewall/rules?origin=manual").json() == []


def test_a_rule_can_be_taken_back_out(open_to_asking: TestClient):
    a_service(open_to_asking)
    open_to_asking.post("/v1/firewall/open", json={"service": "shop-api"})
    assert open_to_asking.delete("/v1/firewall/rules/allow-shop-api").status_code == 204
    assert open_to_asking.get("/v1/firewall/rules").json() == []


def test_taking_out_a_rule_that_is_not_there_is_a_404(open_to_asking: TestClient):
    assert open_to_asking.delete("/v1/firewall/rules/nothing").status_code == 404


def test_a_comment_that_could_be_read_as_a_rule_is_refused(open_to_asking: TestClient):
    """A newline in a comment is a second line in somebody's ruleset."""
    a_service(open_to_asking)
    response = open_to_asking.post(
        "/v1/firewall/open",
        json={"service": "shop-api", "comment": "fine\ntcp dport 22 accept"},
    )
    assert response.status_code == 422


def test_reading_still_needs_a_token_when_one_is_set(settings: Settings):
    with TestClient(create_app(allowing(settings, token="letmein"))) as client:
        assert client.get("/v1/firewall").status_code == 401
        allowed = client.get("/v1/firewall", headers={"Authorization": "Bearer letmein"})
        assert allowed.status_code == 200


class Borrowed(WardenClient):
    """The real client, talking to the app in this process."""

    def __init__(self, served: TestClient) -> None:
        self._http = served

    def close(self) -> None:
        return None


def test_the_client_opens_a_port_and_takes_it_back_out(open_to_asking: TestClient):
    """The whole round trip somebody writing against warden actually makes."""
    a_service(open_to_asking)
    client = Borrowed(open_to_asking)

    rule = client.firewall_open("shop-api", comment="the shop, from the office")
    assert rule.ports == {8000}
    assert rule.source == "10.0.0.0/8"
    assert rule.comment == "the shop, from the office"

    assert [one.name for one in client.firewall_rules()] == ["allow-shop-api"]
    assert client.firewall().from_registry == 1

    client.firewall_close(rule.name)
    assert client.firewall_rules() == []


def test_the_client_is_told_no_when_the_switch_is_off(client: TestClient):
    a_service(client)
    with pytest.raises(NotPermittedError, match="allow_remote_firewall"):
        Borrowed(client).firewall_open("shop-api")


def test_doctor_fails_a_warden_that_takes_no_token_and_will_change_its_firewall():
    """Reachable, unauthenticated and willing is a way through, not a firewall."""
    exposed = Settings(
        host="0.0.0.0", token=None, allow_remote_firewall=True, update_check=False
    )
    checks = health._who_may_change_it(exposed)
    assert checks[0].level == health.FAIL
    assert "allow_remote_firewall" in checks[0].text


def test_a_token_makes_it_a_note_rather_than_a_failure():
    guarded = Settings(
        host="0.0.0.0", token="letmein", allow_remote_firewall=True, update_check=False
    )
    assert health._who_may_change_it(guarded)[0].level == health.NOTE


def test_the_switch_being_off_is_worth_saying_nothing_about():
    assert health._who_may_change_it(Settings(update_check=False)) == []

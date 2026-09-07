"""Tokens that reach only as far as they say, and a history that says who asked."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from warden.api import create_app
from warden.core import asking
from warden.core.config import Settings


def with_tokens(settings: Settings, tokens: str, **more: object) -> Settings:
    # Validated rather than copied: `model_copy` does not run the parser, and a
    # string where a list of grants belongs would only fail much later.
    return Settings.model_validate(
        {**settings.model_dump(), "token": None, "tokens": tokens, **more}
    )


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def scoped(settings: Settings) -> Iterator[TestClient]:
    """A warden with one token per thing somebody might be allowed to do."""
    said = with_tokens(
        settings,
        "reader:read:look, deploy:registry:ship, walls:firewall:brick, boss:all:everything",
        allow_remote_firewall=True,
        firewall_from_registry=True,
        firewall_allow_from={"10.0.0.0/8"},
    )
    with TestClient(create_app(said)) as client:
        yield client


def test_a_reading_token_reads(scoped: TestClient):
    assert scoped.get("/v1/services", headers=bearer("look")).status_code == 200
    assert scoped.get("/v1/pool", headers=bearer("look")).status_code == 200


def test_a_reading_token_does_not_register(scoped: TestClient):
    refused = scoped.post(
        "/v1/services", json={"name": "api", "kind": "backend"}, headers=bearer("look")
    )
    assert refused.status_code == 403
    assert "'reader' may only read" in refused.json()["detail"]


def test_a_registry_token_registers_and_does_not_touch_the_firewall(scoped: TestClient):
    made = scoped.post(
        "/v1/services",
        json={"name": "shop-api", "kind": "backend", "host": "10.4.0.7"},
        headers=bearer("ship"),
    )
    assert made.status_code == 201

    refused = scoped.post(
        "/v1/firewall/rules", json={"what": "ssh"}, headers=bearer("ship")
    )
    assert refused.status_code == 403
    assert "'deploy' may only registry" in refused.json()["detail"]


def test_a_firewall_token_writes_a_rule_and_does_not_register(scoped: TestClient):
    written = scoped.post(
        "/v1/firewall/rules", json={"what": "ssh"}, headers=bearer("brick")
    )
    assert written.status_code == 200

    refused = scoped.post(
        "/v1/services", json={"name": "api", "kind": "backend"}, headers=bearer("brick")
    )
    assert refused.status_code == 403


def test_anything_that_may_change_a_thing_may_also_look_at_it(scoped: TestClient):
    """Reading is the floor, so a scoped token is not blind to what it changes."""
    assert scoped.get("/v1/firewall/rules", headers=bearer("brick")).status_code == 200
    assert scoped.get("/v1/services", headers=bearer("ship")).status_code == 200


def test_one_token_that_reaches_everywhere_still_does(scoped: TestClient):
    assert (
        scoped.post(
            "/v1/services", json={"name": "api", "kind": "backend"}, headers=bearer("everything")
        ).status_code
        == 201
    )
    assert (
        scoped.post(
            "/v1/firewall/rules", json={"what": "https"}, headers=bearer("everything")
        ).status_code
        == 200
    )


def test_a_secret_nobody_wrote_down_is_a_401(scoped: TestClient):
    said = scoped.get("/v1/services", headers=bearer("guessed"))
    assert said.status_code == 401
    assert "invalid or missing token" in said.json()["detail"]


def test_a_single_token_written_the_old_way_still_reaches_everywhere(settings: Settings):
    """A machine set up before any of this existed must not notice it happened."""
    said = settings.model_copy(
        update={"token": "letmein", "allow_remote_firewall": True}
    )
    with TestClient(create_app(said)) as client:
        assert (
            client.post(
                "/v1/services",
                json={"name": "api", "kind": "backend"},
                headers=bearer("letmein"),
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/v1/firewall/rules", json={"what": "ssh"}, headers=bearer("letmein")
            ).status_code
            == 200
        )


def test_nothing_written_down_still_asks_for_nothing(settings: Settings):
    """The loopback default has always been no token and no check."""
    with TestClient(create_app(settings)) as client:
        made = client.post("/v1/services", json={"name": "api", "kind": "backend"})
        assert made.status_code == 201


def test_the_history_says_which_token_asked(scoped: TestClient):
    scoped.post("/v1/services", json={"name": "api", "kind": "backend"}, headers=bearer("ship"))
    said = scoped.get("/v1/history", headers=bearer("look")).json()
    assert said[0]["who"] == "deploy"


def test_something_done_at_the_machine_names_nobody():
    """There is no token at a keyboard, so there is nobody to name."""
    assert asking.who() == ""
    with asking.named("deploy"):
        assert asking.who() == "deploy"
    assert asking.who() == ""


def test_a_secret_can_have_a_colon_in_it():
    said = Settings(tokens="deploy:registry:s3c:ret:more", update_check=False)
    assert said.tokens[0].secret == "s3c:ret:more"
    assert said.tokens[0].scope == "registry"


def test_a_token_with_no_scope_reaches_everywhere():
    said = Settings(tokens="deploy::secret", update_check=False)
    assert said.tokens[0].scope == "all"

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from warden import __version__, webhooks
from warden.models import Event


@pytest.fixture
def event() -> Event:
    return Event(
        at=datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
        action="registered",
        name="shop-api",
        kind="backend",
        project="shop",
        host="127.0.0.1",
        port=8080,
        pid=4242,
    )


def body_of(event: Event, **kwargs) -> dict:
    body, _ = webhooks.render(event, node="build-01", **kwargs)
    return json.loads(body)


def test_the_plain_shape_is_the_event_and_the_warden_that_saw_it(event: Event):
    payload = body_of(event)
    assert payload["action"] == "registered"
    assert payload["name"] == "shop-api"
    assert payload["port"] == 8080
    assert payload["node"] == "build-01"


def test_the_headers_say_what_happened_and_where(event: Event):
    _, headers = webhooks.render(event, node="build-01")
    assert headers["X-Warden-Event"] == "registered"
    assert headers["X-Warden-Node"] == "build-01"
    assert headers["User-Agent"] == f"warden/{__version__}"
    assert webhooks.SIGNATURE not in headers


def test_the_signature_covers_the_bytes_that_are_actually_sent(event: Event):
    body, headers = webhooks.render(event, node="build-01", secret="between-us")
    expected = hmac.new(b"between-us", body, hashlib.sha256).hexdigest()
    assert headers[webhooks.SIGNATURE] == f"sha256={expected}"


def test_a_different_secret_does_not_produce_the_same_signature(event: Event):
    _, mine = webhooks.render(event, node="build-01", secret="between-us")
    _, theirs = webhooks.render(event, node="build-01", secret="between-them")
    assert mine[webhooks.SIGNATURE] != theirs[webhooks.SIGNATURE]


def test_discord_gets_an_embed_a_person_can_read(event: Event):
    embed = body_of(event, shape=webhooks.DISCORD)["embeds"][0]
    assert embed["description"] == "shop-api took 127.0.0.1:8080 on build-01"
    assert embed["color"] == webhooks.COLOURS["registered"]
    assert {"name": "project", "value": "shop", "inline": True} in embed["fields"]


def test_slack_says_it_in_text_as_well_as_blocks(event: Event):
    payload = body_of(event, shape=webhooks.SLACK)
    assert payload["text"] == webhooks.sentence(event, "build-01")
    assert payload["blocks"][0]["text"]["type"] == "mrkdwn"


def test_teams_gets_an_adaptive_card(event: Event):
    attachment = body_of(event, shape=webhooks.TEAMS)["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["text"] == webhooks.sentence(event, "build-01")


def test_every_shape_carries_the_port(event: Event):
    for shape in webhooks.FORMATS:
        body, _ = webhooks.render(event, node="build-01", shape=shape)
        assert b"8080" in body


def test_a_service_without_a_project_does_not_claim_one(event: Event):
    plain = event.model_copy(update={"project": None})
    assert all(name != "project" for name, _ in webhooks.facts(plain, "build-01"))

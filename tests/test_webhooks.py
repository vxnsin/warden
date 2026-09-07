import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from warden import __version__
from warden.core import webhooks
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
    assert embed["color"] == webhooks.LOOKS["port.registered"][0]
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


def node_event(action: str = "stale", **body) -> Event:
    return Event(
        at=datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
        scope="node",
        action=action,
        subject="build-01",
        body=body or {"url": "http://build-01:7010"},
    )


def test_a_node_event_reaches_every_shape():
    for shape in webhooks.FORMATS:
        body, headers = webhooks.render(node_event(), node="hub", shape=shape)
        assert b"build-01" in body
        # The bare action stays where it was; the scope is a field of its own,
        # so a receiver written against 0.2.0 still routes on what it knows.
        assert headers["X-Warden-Event"] == "stale"
        assert headers["X-Warden-Scope"] == "node"


def test_a_node_event_says_what_happened_in_words():
    assert webhooks.sentence(node_event(), "hub") == "build-01 has gone quiet"
    assert webhooks.sentence(node_event("joined"), "hub") == "build-01 reported in"


def test_a_firewall_event_carries_its_own_fields():
    applied = Event(
        at=datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
        scope="firewall",
        action="applied",
        subject="nftables",
        body={"rules": 12, "rollback": 60},
    )
    pairs = dict(webhooks.facts(applied, "hub"))
    assert pairs["what"] == "firewall.applied"
    assert pairs["rules"] == "12"
    assert pairs["node"] == "hub"


def test_each_event_has_its_own_colour_and_words():
    from warden.core.webhooks import looks

    assert looks(node_event("stale"))[1] == "has gone quiet"
    assert looks(node_event("joined"))[0] != looks(node_event("stale"))[0]


def test_a_colour_can_be_overridden_one_event_at_a_time():
    from warden.core.webhooks import looks

    assert looks(node_event("stale"), {"node.stale": "#123456"})[0] == 0x123456
    assert looks(node_event("stale"), {"node.stale": "not a colour"})[0] == 0xA8434A
    assert looks(node_event("joined"), {"node.stale": "#123456"})[0] == 0x4C9A5B


def test_the_words_can_be_overridden_one_event_at_a_time():
    from warden.core.webhooks import looks

    said = {"node.stale": "antwortet nicht mehr"}
    assert looks(node_event("stale"), None, said)[1] == "antwortet nicht mehr"
    assert looks(node_event("joined"), None, said)[1] == "reported in"


def test_the_words_reach_every_shape_that_shows_them():
    said = {"node.stale": "antwortet nicht mehr"}
    for shape in (webhooks.DISCORD, webhooks.SLACK, webhooks.TEAMS):
        body, _ = webhooks.render(node_event(), node="hub", shape=shape, titles=said)
        assert b"antwortet nicht mehr" in body


def test_the_colour_and_the_words_are_named_apart():
    """Setting one must not disturb the other."""
    from warden.core.webhooks import looks

    colour, words = looks(node_event("stale"), {"node.stale": "#123456"}, None)
    assert colour == 0x123456
    assert words == "has gone quiet"

    colour, words = looks(node_event("stale"), None, {"node.stale": "gone"})
    assert colour == 0xA8434A
    assert words == "gone"


def test_the_plain_shape_carries_neither_because_it_carries_the_event():
    """`json` is for something that reads fields, not words."""
    body, _ = webhooks.render(
        node_event(), node="hub", shape=webhooks.JSON, titles={"node.stale": "gone"}
    )
    assert b"gone" not in body
    assert b'"action":"stale"' in body

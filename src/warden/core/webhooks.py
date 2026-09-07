"""Turning an event into something the far end will actually render.

A plain JSON post is the honest default and the thing to build anything else
on. But the place people want to hear that a port was taken is a chat window,
and every chat window insists on its own shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping

from warden import __version__
from warden.models import NODE, PORT, Event

JSON = "json"
DISCORD = "discord"
SLACK = "slack"
TEAMS = "teams"

FORMATS = (JSON, DISCORD, SLACK, TEAMS)

SIGNATURE = "X-Warden-Signature"

VERBS = {
    "registered": "took",
    "renewed": "kept",
    "moved": "moved to",
    "released": "gave up",
    "expired": "lost",
}


# What each one looks like in a chat window. Overridable one at a time, so
# `webhook_colours = "node.stale=#e5544b"` changes that one and leaves the rest.
LOOKS: dict[str, tuple[int, str]] = {
    "port.registered": (0x4C9A5B, "took a port"),
    "port.renewed": (0x4A7EA8, "kept its port"),
    "port.moved": (0xC8892A, "moved"),
    "port.released": (0x6E6E6E, "gave up its port"),
    "port.expired": (0xA8434A, "lost its port"),
    "node.joined": (0x4C9A5B, "reported in"),
    "node.returned": (0x4C9A5B, "is answering again"),
    "node.stale": (0xA8434A, "has gone quiet"),
    "node.forgotten": (0x6E6E6E, "was forgotten"),
    "firewall.applied": (0xC8892A, "ruleset applied"),
    "firewall.confirmed": (0x4C9A5B, "ruleset kept"),
    "firewall.rolled_back": (0xA8434A, "rolled itself back"),
    "firewall.restored": (0x6E6E6E, "snapshot restored"),
}

PLAIN = (0x6E6E6E, "happened")


def looks(
    event: Event,
    colours: Mapping[str, str] | None = None,
    titles: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """The colour and the words for one event, with any overrides applied.

    Both are named one event at a time, so setting `node.stale` leaves the
    other twelve with the ones they came with.
    """
    colour, said = LOOKS.get(event.full, PLAIN)
    chosen = (colours or {}).get(event.full)
    if chosen:
        colour = _colour(chosen, colour)
    return colour, (titles or {}).get(event.full) or said


def _colour(said: str, fallback: int) -> int:
    """`#4c9a5b` or `4c9a5b`. Anything else keeps the one it came with, because
    a colour Discord refuses would lose the message rather than the colour."""
    try:
        return int(said.lstrip("#"), 16)
    except ValueError:
        return fallback

def sentence(
    event: Event, node: str, titles: Mapping[str, str] | None = None
) -> str:
    """One line, readable by someone who has never heard of warden."""
    _, title = looks(event, None, titles)
    if event.scope == PORT:
        return f"{event.name} {VERBS.get(event.action, event.action)} {event.address} on {node}"
    where = f" on {node}" if event.scope != NODE else ""
    return f"{event.subject or event.scope} {title}{where}"


def _title(event: Event) -> str:
    """What the message calls itself: the event, and what it happened to."""
    return f"{event.full} - {event.subject}" if event.subject else event.full


def facts(event: Event, node: str) -> list[tuple[str, str]]:
    """The fields worth showing, which depend on what kind of thing this is."""
    if event.scope != PORT:
        pairs = [(key, str(said)) for key, said in event.body.items() if said not in (None, "")]
        return [("what", event.full), *pairs, ("node", node)]

    pairs = [("service", event.name), ("kind", event.kind)]
    if event.project:
        pairs.append(("project", event.project))
    pairs.append(("address", event.address))
    if event.pid:
        pairs.append(("pid", str(event.pid)))
    pairs.append(("node", node))
    return pairs


# What a port event carries and nothing else does. Sending them empty on a
# node event would be noise a reader has to learn to ignore.
OF_A_PORT = ("name", "kind", "project", "host", "port", "pid")


def _plain(
    event: Event,
    node: str,
    colours: Mapping[str, str] | None = None,
    titles: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """The event as it is, with only the fields this kind of event has.

    A port event keeps the shape 0.2.0 sent, down to the field order, so a
    reader written against it does not notice that anything widened.
    """
    payload = event.model_dump(mode="json")
    if event.scope != PORT:
        for field in OF_A_PORT:
            payload.pop(field, None)
    elif not event.body:
        payload.pop("body", None)
    payload["node"] = node
    return payload


def _discord(
    event: Event,
    node: str,
    colours: Mapping[str, str] | None = None,
    titles: Mapping[str, str] | None = None,
) -> dict[str, object]:
    return {
        "embeds": [
            {
                "title": _title(event),
                "description": sentence(event, node, titles),
                "color": looks(event, colours, titles)[0],
                "timestamp": event.at.isoformat(),
                "fields": [
                    {"name": name, "value": value, "inline": True}
                    for name, value in facts(event, node)
                ],
            }
        ]
    }


def _slack(
    event: Event,
    node: str,
    colours: Mapping[str, str] | None = None,
    titles: Mapping[str, str] | None = None,
) -> dict[str, object]:
    # `text` as well as `blocks`, because that is what a phone notification
    # shows and what a client too old for blocks falls back to.
    return {
        "text": sentence(event, node, titles),
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{event.action}* {sentence(event, node, titles)}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": " | ".join(
                            f"{name}: `{value}`" for name, value in facts(event, node)
                        ),
                    }
                ],
            },
        ],
    }


def _teams(
    event: Event,
    node: str,
    colours: Mapping[str, str] | None = None,
    titles: Mapping[str, str] | None = None,
) -> dict[str, object]:
    # An adaptive card inside a message, which is what a Power Automate flow
    # accepts. The old Office 365 connector card is on its way out.
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": sentence(event, node, titles),
                            "weight": "Bolder",
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": name, "value": value}
                                for name, value in facts(event, node)
                            ],
                        },
                    ],
                },
            }
        ],
    }


BUILDERS = {JSON: _plain, DISCORD: _discord, SLACK: _slack, TEAMS: _teams}


def render(
    event: Event,
    *,
    node: str,
    shape: str = JSON,
    secret: str | None = None,
    colours: Mapping[str, str] | None = None,
    titles: Mapping[str, str] | None = None,
) -> tuple[bytes, dict[str, str]]:
    """The bytes to post, and the headers to post them with.

    Serialised here rather than left to the HTTP client, because a signature
    over a body somebody else re-serialises signs something else.
    """
    payload = BUILDERS[shape](event, node, colours, titles)
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"warden/{__version__}",
        "X-Warden-Node": node,
        # The bare action, as it has always been, so a receiver written
        # against 0.2.0 keeps routing on it. The scope is beside it.
        "X-Warden-Event": event.action,
        "X-Warden-Scope": event.scope,
    }
    if secret:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers[SIGNATURE] = f"sha256={digest}"
    return body, headers

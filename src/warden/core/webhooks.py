"""Turning an event into something the far end will actually render.

A plain JSON post is the honest default and the thing to build anything else
on. But the place people want to hear that a port was taken is a chat window,
and every chat window insists on its own shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from warden import __version__
from warden.models import Event

JSON = "json"
DISCORD = "discord"
SLACK = "slack"
TEAMS = "teams"

FORMATS = (JSON, DISCORD, SLACK, TEAMS)

SIGNATURE = "X-Warden-Signature"

COLOURS = {
    "registered": 0x4C9A5B,
    "moved": 0xC8892A,
    "renewed": 0x4A7EA8,
    "released": 0x6E6E6E,
    "expired": 0xA8434A,
}

VERBS = {
    "registered": "took",
    "renewed": "kept",
    "moved": "moved to",
    "released": "gave up",
    "expired": "lost",
}


def sentence(event: Event, node: str) -> str:
    """One line, readable by someone who has never heard of warden."""
    verb = VERBS.get(event.action, event.action)
    return f"{event.name} {verb} {event.address} on {node}"


def facts(event: Event, node: str) -> list[tuple[str, str]]:
    pairs = [("service", event.name), ("kind", event.kind)]
    if event.project:
        pairs.append(("project", event.project))
    pairs.append(("address", event.address))
    if event.pid:
        pairs.append(("pid", str(event.pid)))
    pairs.append(("node", node))
    return pairs


def _plain(event: Event, node: str) -> dict[str, object]:
    payload = event.model_dump(mode="json")
    payload["node"] = node
    return payload


def _discord(event: Event, node: str) -> dict[str, object]:
    return {
        "embeds": [
            {
                "title": f"{event.action} - {event.name}",
                "description": sentence(event, node),
                "color": COLOURS.get(event.action, COLOURS["released"]),
                "timestamp": event.at.isoformat(),
                "fields": [
                    {"name": name, "value": value, "inline": True}
                    for name, value in facts(event, node)
                ],
            }
        ]
    }


def _slack(event: Event, node: str) -> dict[str, object]:
    # `text` as well as `blocks`, because that is what a phone notification
    # shows and what a client too old for blocks falls back to.
    return {
        "text": sentence(event, node),
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{event.action}* {sentence(event, node)}"},
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


def _teams(event: Event, node: str) -> dict[str, object]:
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
                            "text": sentence(event, node),
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
    event: Event, *, node: str, shape: str = JSON, secret: str | None = None
) -> tuple[bytes, dict[str, str]]:
    """The bytes to post, and the headers to post them with.

    Serialised here rather than left to the HTTP client, because a signature
    over a body somebody else re-serialises signs something else.
    """
    payload = BUILDERS[shape](event, node)
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"warden/{__version__}",
        "X-Warden-Node": node,
        "X-Warden-Event": event.action,
    }
    if secret:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers[SIGNATURE] = f"sha256={digest}"
    return body, headers

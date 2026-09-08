"""Doctor on a timer: it says a finding changed, not what it found."""

import json

import pytest
from fastapi.testclient import TestClient

from warden.api import create_app
from warden.core import happenings, webhooks
from warden.core.config import Settings
from warden.core.health import FAIL, NOTE, OK, WARN, Check, examine
from warden.core.rounds import Rounds
from warden.core.store import Store
from warden.models import HEALTH


class Book:
    """A store that keeps what it was told rather than writing it down."""

    def __init__(self) -> None:
        self.said: list[tuple[str, str, str, dict]] = []

    def announce(self, scope: str, action: str, subject: str, **body: object) -> None:
        self.said.append((scope, action, subject, body))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(database=tmp_path / "warden.db", node="here", update_check=False)


def rounds(settings: Settings) -> tuple[Rounds, Book]:
    book = Book()
    return Rounds(settings, book, client=None), book


def firewall(level: str = WARN, text: str = "3 rules changed since the last apply") -> Check:
    return Check(level, text, "firewall")


def test_the_first_look_is_quiet(settings: Settings):
    """What a machine was already like is not news; a restart must not be a message."""
    watch, book = rounds(settings)
    assert watch.notice([firewall()]) == []
    assert book.said == []


def test_a_finding_that_appears_is_announced_once(settings: Settings):
    watch, book = rounds(settings)
    watch.notice([Check(OK, "nothing since the last apply", "firewall")])

    assert watch.notice([firewall()]) == [("worsened", "firewall")]
    assert watch.notice([firewall()]) == []
    assert watch.notice([firewall()]) == []

    scope, action, subject, body = book.said[0]
    assert (scope, action, subject) == (HEALTH, "worsened", "firewall")
    assert body == {"level": WARN, "says": "3 rules changed since the last apply"}


def test_a_finding_that_goes_away_is_announced_too(settings: Settings):
    watch, book = rounds(settings)
    watch.notice([Check(OK, "nothing since the last apply", "firewall")])
    watch.notice([firewall()])

    said = watch.notice([Check(OK, "nothing since the last apply", "firewall")])
    assert said == [("recovered", "firewall")]
    assert book.said[-1] == (
        HEALTH,
        "recovered",
        "firewall",
        {"level": OK, "says": "nothing since the last apply"},
    )


def test_a_warning_that_becomes_a_failure_is_worth_saying_again(settings: Settings):
    watch, book = rounds(settings)
    watch.notice([Check(OK, "pool 8000-8999, 3 held", "pool")])
    watch.notice([Check(WARN, "nearly out", "pool")])

    assert watch.notice([Check(FAIL, "the pool is full", "pool")]) == [("worsened", "pool")]
    assert [one[3]["level"] for one in book.said] == [WARN, FAIL]


def test_a_note_is_not_something_being_wrong(settings: Settings):
    """A channel told at three in the morning that a newer warden exists is a muted channel."""
    watch, book = rounds(settings)
    watch.notice([Check(OK, "answering", "updates")])
    assert watch.notice([Check(NOTE, "a newer warden exists", "updates")]) == []
    assert book.said == []


def test_each_check_is_followed_on_its_own(settings: Settings):
    watch, _ = rounds(settings)
    watch.notice([Check(OK, "-", "pool"), Check(OK, "-", "firewall")])

    said = watch.notice([Check(WARN, "nearly out", "pool"), Check(OK, "-", "firewall")])
    assert said == [("worsened", "pool")]
    assert watch.notice([Check(WARN, "nearly out", "pool"), firewall()]) == [
        ("worsened", "firewall")
    ]


def test_the_worst_thing_one_check_said_is_the_one_that_travels(settings: Settings):
    watch, book = rounds(settings)
    watch.notice([])
    watch.notice(
        [
            Check(WARN, "3 rules changed", "firewall"),
            Check(FAIL, "nftables is not on this machine", "firewall"),
        ]
    )
    assert book.said[-1][3] == {"level": FAIL, "says": "nftables is not on this machine"}


def test_a_check_it_cannot_run_is_not_a_finding_appearing(settings: Settings):
    """A machine that stops answering at all must not read as ten things going wrong."""
    watch, book = rounds(settings)
    watch.notice([Check(OK, "answering", "answering")])
    watch.notice([Check(FAIL, "nobody is answering at http://127.0.0.1:7010", "answering")])
    assert [one[2] for one in book.said] == ["answering"]


# What the checks are, and what it costs to run them


def test_every_check_says_which_one_it_was(settings: Settings):
    with TestClient(create_app(settings)) as client:
        checks = examine(_here(client), settings)
    assert all(check.about for check in checks)
    assert {"answering", "settings", "pool", "services"} <= {one.about for one in checks}


def test_the_timer_does_not_walk_the_machines_sockets(settings: Settings, monkeypatch):
    """Ten minutes apart, forever, is a cost `/metrics` already refuses to pay."""
    swept = []
    monkeypatch.setattr("warden.ports.service.bound_ports", lambda: swept.append(1) or set())
    with TestClient(create_app(settings)) as client:
        client.post("/v1/services", json={"name": "shop-api", "kind": "backend"})
        examine(_here(client), settings, sweeping=False)
        assert swept == []
        examine(_here(client), settings)
        assert swept == [1]


def _here(client: TestClient):
    return client.app.state.rounds.client


# What a person sees in the channel


def test_the_two_of_them_are_things_somebody_can_subscribe_to():
    assert "health.worsened" in happenings.NAMES
    assert "health.recovered" in happenings.NAMES
    assert happenings.known("health.worsened") == "health.worsened"
    assert "health.worsened" in happenings.NOTABLE


def test_they_have_words_and_a_colour_of_their_own():
    for name in ("health.worsened", "health.recovered"):
        event = happenings.like(name)
        raw, _ = webhooks.render(event, node="hub", shape="discord")
        embed = json.loads(raw)["embeds"][0]
        assert embed["author"]["name"] == name
        assert embed["title"].endswith("firewall")
        assert "firewall" not in embed["description"]  # the title already said it

    worsened = json.loads(webhooks.render(happenings.like("health.worsened"), node="hub")[0])
    assert worsened["body"]["says"] == "3 rules changed since the last apply"


def test_the_watch_can_be_turned_off(tmp_path):
    said = Settings(
        database=tmp_path / "w.db", update_check=False, health_watch=False
    )
    with TestClient(create_app(said)) as client:
        assert client.app.state.rounds._task is None


def test_it_is_running_by_default(settings: Settings):
    with TestClient(create_app(settings)) as client:
        assert client.app.state.rounds._task is not None


def test_it_looks_minutes_apart_rather_than_seconds(settings: Settings):
    assert settings.health_interval >= 60
    with pytest.raises(ValueError, match="health_interval"):
        Settings(database=settings.database, health_interval=5)


def test_a_finding_reaches_the_book_and_the_listeners(tmp_path, settings: Settings):
    """The same book, the same webhook - nobody should have to look in two places."""
    with Store(tmp_path / "events.db") as store:
        heard = []
        store.subscribe(heard.append)
        watch = Rounds(settings, store, client=None)
        watch.notice([Check(OK, "-", "pool")])
        watch.notice([Check(FAIL, "the pool is full", "pool")])

        assert [one.full for one in heard] == ["health.worsened"]
        assert heard[0].subject == "pool"
        assert store.history(limit=5)[0].scope == HEALTH

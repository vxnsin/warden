"""Taking a machine's registrations and rules somewhere else."""

import json
from datetime import UTC, datetime

import pytest

from warden.core import moving
from warden.core.config import Settings
from warden.core.store import RuleStore, Store
from warden.errors import WardenError
from warden.firewall.model import Rule
from warden.models import Registration

NOW = datetime.now(UTC)


def settings(**more) -> Settings:
    said = {"pool_start": 8000, "pool_end": 8004, "update_check": False}
    return Settings(**{**said, **more})


def service(name: str, port: int) -> Registration:
    return Registration(
        name=name,
        kind="backend",
        host="127.0.0.1",
        port=port,
        project=None,
        pid=None,
        meta={},
        ttl=None,
        created_at=NOW,
        updated_at=NOW,
        expires_at=None,
    )


@pytest.fixture
def books(tmp_path):
    with Store(tmp_path / "registry.db") as store:
        yield store


def test_what_travels_is_what_somebody_wrote_down(books):
    books.save(service("shop-api", 8000))
    RuleStore(books).save(Rule(name="allow-ssh", ports={22}))

    said = moving.taken(books)
    assert [one["name"] for one in said["services"]] == ["shop-api"]
    assert [one["name"] for one in said["rules"]] == ["allow-ssh"]
    assert said["shape"] == moving.SHAPE


def test_the_history_and_the_snapshots_stay_where_they_happened(books):
    """A history means nothing elsewhere, and a snapshot is another machine's."""
    books.save(service("shop-api", 8000))
    said = moving.taken(books)
    assert set(said) == {"shape", "written_by", "at", "services", "rules"}


def test_a_file_warden_did_not_write_is_refused_by_name():
    for said in ("not json at all", "[]", '{"services": []}'):
        with pytest.raises(WardenError, match="that is not the file warden writes"):
            moving.read(said)


def test_a_shape_from_a_later_warden_is_refused_rather_than_guessed_at():
    said = json.dumps({"shape": moving.SHAPE + 1, "written_by": "9.9.9"})
    with pytest.raises(WardenError, match="shape"):
        moving.read(said)


def test_a_round_trip_keeps_everything_that_matters(books, tmp_path):
    books.save(service("shop-api", 8000))
    RuleStore(books).save(Rule(name="allow-ssh", ports={22}, limit="6/minute"))
    said = json.dumps(moving.taken(books))

    with Store(tmp_path / "other.db") as elsewhere:
        services, rules = moving.read(said)
        landed = moving.land(elsewhere, services, rules, settings=settings())
        assert landed.services == ["shop-api"]
        assert landed.rules == ["allow-ssh"]
        assert [one.port for one in elsewhere.list()] == [8000]
        assert RuleStore(elsewhere).list()[0].limit == "6/minute"


def test_a_name_already_here_is_skipped_by_name_and_the_rest_still_lands(books):
    books.save(service("shop-api", 8000))
    landed = moving.land(
        books,
        [service("shop-api", 8001), service("docs", 8002)],
        [],
        settings=settings(),
    )
    assert landed.services == ["docs"]
    assert landed.skipped == ["shop-api - already registered here"]
    assert {one.name for one in books.list()} == {"shop-api", "docs"}


def test_a_port_this_machine_does_not_hand_out_is_skipped(books):
    """The pool is this machine's, and a registration does not get to widen it."""
    landed = moving.land(books, [service("shop-api", 9999)], [], settings=settings())
    assert landed.services == []
    assert "outside 8000-8004" in landed.skipped[0]


def test_any_port_lets_it_land_where_it_can(books):
    landed = moving.land(
        books, [service("shop-api", 9999)], [], settings=settings(), keep_ports=False
    )
    assert landed.services == ["shop-api"]


def test_a_port_held_here_is_skipped_even_inside_the_pool(books):
    books.save(service("already", 8000))
    landed = moving.land(books, [service("shop-api", 8000)], [], settings=settings())
    assert landed.services == []
    assert "is held here" in landed.skipped[0]


def test_a_dry_run_says_what_would_happen_and_writes_nothing(books):
    landed = moving.land(
        books,
        [service("shop-api", 8000)],
        [Rule(name="allow-ssh", ports={22})],
        settings=settings(),
        dry_run=True,
    )
    assert landed.services == ["shop-api"]
    assert landed.rules == ["allow-ssh"]
    assert books.list() == []
    assert RuleStore(books).list() == []


def test_a_rule_already_written_down_here_is_skipped(books):
    RuleStore(books).save(Rule(name="allow-ssh", ports={22}))
    landed = moving.land(
        books, [], [Rule(name="allow-ssh", ports={2222})], settings=settings()
    )
    assert landed.rules == []
    assert "already written down here" in landed.skipped[0]
    assert RuleStore(books).list()[0].ports == {22}

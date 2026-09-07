"""The firewall's numbers, where a fleet can be watched rather than walked."""

import os

import pytest
from fastapi.testclient import TestClient

from warden.api import create_app
from warden.core.config import Settings
from warden.core.store import RuleStore, Snapshots, Store
from warden.firewall import guard
from warden.firewall.model import Origin, Policy, Rule


@pytest.fixture
def scraped(tmp_path):
    """A warden that may be asked, and a scrape of what it says."""
    said = Settings(
        database=tmp_path / "registry.db", update_check=False, allow_remote_firewall=True
    )

    def scrape() -> str:
        with TestClient(create_app(said)) as client:
            return client.get("/metrics").text

    return said, scrape


def numbers(text: str) -> dict[str, float]:
    found = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, value = line.rpartition(" ")
        found[name] = float(value)
    return found


def test_a_warden_with_no_rules_still_says_so(scraped):
    _, scrape = scraped
    said = numbers(scrape())
    assert said["warden_firewall_rules"] == 0
    assert said["warden_firewall_rules_live"] == 0


def test_rules_are_counted_by_where_they_came_from(scraped):
    settings, scrape = scraped
    with Store(settings.database) as store:
        rules = RuleStore(store)
        rules.save(Rule(name="allow-ssh", ports={22}, origin=Origin.CATALOGUE))
        rules.save(Rule(name="allow-8080", ports={8080}, origin=Origin.MANUAL))
        rules.save(Rule(name="allow-8443", ports={8443}, origin=Origin.MANUAL))

    said = numbers(scrape())
    assert said['warden_firewall_rules{origin="manual"}'] == 2
    assert said['warden_firewall_rules{origin="catalogue"}'] == 1
    assert said["warden_firewall_rules_live"] == 3


def test_what_is_written_down_and_not_applied_is_a_number_too(scraped):
    settings, scrape = scraped
    with Store(settings.database) as store:
        RuleStore(store).save(Rule(name="allow-ssh", ports={22}))
    assert numbers(scrape())["warden_firewall_pending"] == 1


def test_applying_moves_the_counter_and_clears_the_pending(scraped):
    settings, scrape = scraped

    class Pretend:
        kind = "nftables"

        def snapshot(self) -> str:
            return "before"

        def apply(self, policy) -> None:
            return None

    with Store(settings.database) as store:
        rules = RuleStore(store)
        rules.save(Rule(name="allow-ssh", ports={22}))
        guard.apply(Pretend(), Snapshots(store), Policy(rules=rules.list()), rollback=0)

    said = numbers(scrape())
    assert said["warden_firewall_pending"] == 0
    assert said["warden_firewall_applied_total"] == 1
    assert said["warden_firewall_rollback_armed"] == 0


def test_the_totals_survive_the_history_being_trimmed(scraped):
    """A counter that quietly starts again is a counter that lies."""
    settings, scrape = scraped
    with Store(settings.database) as store:
        snapshots = Snapshots(store)
        snapshots.tally(guard.APPLIED)
        snapshots.tally(guard.APPLIED)
        store._db.execute("DELETE FROM events")
        store._db.commit()

    assert numbers(scrape())["warden_firewall_applied_total"] == 2


def test_an_armed_rollback_is_visible_without_asking_the_machine(scraped):
    from datetime import UTC, datetime, timedelta

    settings, scrape = scraped
    with Store(settings.database) as store:
        snapshots = Snapshots(store)
        which = snapshots.take("nftables", "before", "applying a policy")
        snapshots.arm(which, datetime.now(UTC) + timedelta(minutes=2), "applying a policy")

    assert numbers(scrape())["warden_firewall_rollback_armed"] == 1


def test_the_scrape_needs_a_token_where_one_is_set(tmp_path):
    said = Settings(
        database=tmp_path / "registry.db", update_check=False, token="letmein"
    )
    with TestClient(create_app(said)) as client:
        assert client.get("/metrics").status_code == 401
        allowed = client.get("/metrics", headers={"Authorization": "Bearer letmein"})
        assert allowed.status_code == 200
        assert "warden_firewall_rules" in allowed.text


def test_nothing_here_asks_the_kernel(scraped):
    """A scrape every fifteen seconds must not sweep the machine.

    Every number comes out of the store, so a machine with no firewall backend
    at all still answers - which is also what makes this testable anywhere.
    """
    _, scrape = scraped
    assert os.environ.get("WARDEN_FIREWALL_BACKEND") is None
    assert "warden_firewall_rules" in scrape()

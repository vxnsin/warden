"""A rate on a rule, in every dialect that has one and refused where there is none."""

import pytest
from pydantic import ValidationError

from warden.errors import NotPermittedError
from warden.firewall import adopt
from warden.firewall.backends import iptables, nftables, pf, windows
from warden.firewall.model import Action, Direction, Protocol, Rule, per_second


def rule(**more) -> Rule:
    said = {
        "name": "allow-ssh",
        "direction": Direction.IN,
        "action": Action.ALLOW,
        "protocol": Protocol.TCP,
        "ports": {22},
        "source": "10.0.0.0/8",
    }
    return Rule(**{**said, **more})


def test_a_rate_is_a_count_and_a_span():
    assert rule(limit="10/second").limit == "10/second"
    assert per_second("6/minute") == (6, 60)
    assert per_second("2/hour") == (2, 3600)


def test_anything_else_is_refused_at_the_model():
    """A field somebody can write anything into ends up in a ruleset."""
    for said in ("lots", "10/fortnight", "0/second", "-1/minute", "10 / second", ""):
        with pytest.raises(ValidationError):
            rule(limit=said)


def test_nftables_puts_the_rate_before_the_verdict():
    """What exceeds it should fall through to the policy, not be let past fast."""
    said = nftables.line(rule(limit="6/minute"))
    assert "limit rate 6/minute accept" in said


def test_iptables_says_it_as_a_match():
    assert "-m limit --limit 6/minute" in iptables.line(rule(limit="6/minute"))


def test_pf_counts_per_source_because_that_is_what_it_has():
    said = pf.line(rule(limit="6/minute"))
    assert "max-src-conn-rate 6/60" in said


def test_a_rule_without_one_is_written_exactly_as_before():
    plain = rule()
    assert "limit" not in nftables.line(plain)
    assert "--limit" not in iptables.line(plain)
    assert pf.line(plain).count("keep state") == 1


def test_windows_refuses_by_name_rather_than_dropping_the_limit():
    """The same rule letting far more through here, with nothing saying so."""
    with pytest.raises(NotPermittedError, match="no rate limit"):
        windows.line(rule(limit="6/minute"))


def test_windows_is_untouched_by_a_rule_that_has_none():
    assert "localport=22" in windows.line(rule())


UFW = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     LIMIT IN    Anywhere
[ 2] 80/tcp                     ALLOW IN    Anywhere
"""


def test_a_ufw_limit_rule_is_carried_over_rather_than_dropped():
    """It used to be named in the report and lost. A named loss is still a loss."""
    said = adopt.from_ufw(UFW)
    limited = [one for one in said.rules if 22 in one.ports]
    assert limited and limited[0].limit == adopt.UFW_LIMIT
    assert limited[0].action is Action.ALLOW
    assert said.untranslated == []


def test_the_ufw_line_it_came_from_is_kept_in_the_comment():
    """The rate is the nearest warden can say, so the original is worth keeping."""
    said = adopt.from_ufw(UFW)
    limited = next(one for one in said.rules if 22 in one.ports)
    assert "LIMIT" in (limited.comment or "")


def test_an_ordinary_ufw_rule_still_has_no_limit():
    said = adopt.from_ufw(UFW)
    plain = next(one for one in said.rules if 80 in one.ports)
    assert plain.limit is None


def test_a_limit_survives_being_written_down_and_read_back(tmp_path):
    from warden.core.store import RuleStore, Store

    with Store(tmp_path / "rules.db") as store:
        rules = RuleStore(store)
        rules.save(rule(limit="6/minute"))
        assert rules.list()[0].limit == "6/minute"


def test_an_older_database_gains_the_column_and_keeps_its_rules(tmp_path):
    import sqlite3

    where = tmp_path / "rules.db"
    from warden.core.store import RuleStore, Store

    with Store(where) as store:
        RuleStore(store).save(rule())

    # Take the column away again, the way a database written by 0.5.1 has it.
    with sqlite3.connect(where) as db:
        db.execute("ALTER TABLE rules DROP COLUMN \"limit\"")

    with Store(where) as store:
        found = RuleStore(store).list()
        assert [one.name for one in found] == ["allow-ssh"]
        assert found[0].limit is None


def allowing(settings):
    """A warden that may be asked, with a database of its own."""
    return settings.model_copy(update={"allow_remote_firewall": True})


def test_a_rate_can_be_asked_for_over_the_api(settings):
    from fastapi.testclient import TestClient

    from warden.api import create_app

    with TestClient(create_app(allowing(settings))) as client:
        written = client.post(
            "/v1/firewall/rules", json={"what": "ssh", "limit": "6/minute"}
        )
        assert written.status_code == 200
        assert written.json()["limit"] == "6/minute"


def test_a_rate_that_is_not_one_is_refused_before_it_is_written(settings):
    from fastapi.testclient import TestClient

    from warden.api import create_app

    with TestClient(create_app(allowing(settings))) as client:
        refused = client.post(
            "/v1/firewall/rules", json={"what": "ssh", "limit": "lots"}
        )
        assert refused.status_code == 422
        assert client.get("/v1/firewall/rules").json() == []

"""Every firewall stops at the first rule that matches, so the order is the ruleset."""

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from warden.cli import app
from warden.core.store import RuleStore, Store
from warden.firewall import catalogue
from warden.firewall.model import (
    Action,
    Direction,
    Policy,
    Protocol,
    Rule,
    covers,
    placed,
    shadowed,
)


def rule(name: str, ports: set[int], **more) -> Rule:
    said = {"name": name, "ports": ports, "source": "any", "priority": 100}
    return Rule(**{**said, **more})


def pool(**more) -> Rule:
    return rule("allow-pool", set(range(8000, 9000)), **{"source": "10.0.0.0/8", **more})


def test_a_wider_allow_covers_a_narrower_deny():
    """The case somebody actually writes, and the one that costs them."""
    assert covers(pool(), rule("deny-8080", {8080}, action=Action.DENY, source="10.1.0.0/16"))


def test_a_narrower_rule_does_not_cover_a_wider_one():
    assert not covers(rule("deny-8080", {8080}), pool())


def test_a_different_direction_never_covers():
    out = pool(direction=Direction.OUT)
    assert not covers(out, rule("deny-8080", {8080}))


def test_a_different_protocol_never_covers():
    udp = pool(protocol=Protocol.UDP)
    assert not covers(udp, rule("deny-8080", {8080}, protocol=Protocol.TCP))


def test_any_protocol_covers_a_named_one():
    """`any` carries no ports, which is what makes it the widest rule there is."""
    both = Rule(name="allow-all", ports=set(), protocol=Protocol.ANY, source="10.0.0.0/8")
    assert covers(both, rule("deny-8080", {8080}, source="10.1.0.0/16"))


def test_a_source_outside_the_wider_one_is_not_covered():
    assert not covers(pool(), rule("deny-8080", {8080}, source="192.168.0.0/16"))


def test_neither_family_covers_the_other():
    v6 = pool(source="2001:db8::/32")
    assert not covers(v6, rule("deny-8080", {8080}, source="10.1.0.0/16"))


def test_anywhere_covers_everything_and_nothing_covers_anywhere():
    wide = pool(source="any")
    assert covers(wide, rule("deny-8080", {8080}, source="10.1.0.0/16"))
    narrow = pool(source="10.0.0.0/8")
    assert not covers(narrow, rule("deny-8080", {8080}, source="any"))


def test_the_rule_underneath_is_the_one_named():
    hidden = rule("deny-8080", {8080}, action=Action.DENY, source="10.1.0.0/16", priority=200)
    found = shadowed([pool(priority=100), hidden])
    assert [(one.name, by.name) for one, by in found] == [("deny-8080", "allow-pool")]


def test_moving_it_in_front_is_the_whole_fix():
    hidden = rule("deny-8080", {8080}, action=Action.DENY, source="10.1.0.0/16", priority=50)
    assert shadowed(Policy(rules=[pool(), hidden]).live(datetime.now(UTC))) == []


def test_a_policy_hands_them_over_in_the_order_they_decide():
    """Sorted where the ruleset is built, not left to whoever made the list."""
    said = Policy(
        rules=[rule("third", {3}, priority=300), rule("first", {1}, priority=100)]
    ).live(datetime.now(UTC))
    assert [one.name for one in said] == ["first", "third"]


def test_a_rule_nobody_placed_goes_at_the_end():
    have = [rule("allow-ssh", {22}, priority=100), pool(priority=110)]
    said = placed(have, rule("deny-8080", {8080}))
    assert said[0].priority == 120
    assert len(said) == 1


def test_before_puts_it_in_front_and_moves_what_was_there():
    have = [rule("allow-ssh", {22}, priority=100), pool(priority=110)]
    said = placed(have, rule("deny-8080", {8080}), before="allow-pool")
    moved = {one.name: one.priority for one in said}
    assert moved["deny-8080"] < moved["allow-pool"]
    assert "allow-ssh" not in moved  # nothing before it had to move


def test_after_puts_it_behind():
    have = [rule("allow-ssh", {22}, priority=100), pool(priority=110)]
    said = placed(have, rule("deny-8080", {8080}), after="allow-ssh")
    moved = {one.name: one.priority for one in said}
    assert 100 < moved["deny-8080"] < moved["allow-pool"]


def test_a_rule_that_is_not_there_to_go_before_is_refused_by_name():
    with pytest.raises(ValueError, match="no rule called 'nothing'"):
        placed([], rule("deny-8080", {8080}), before="nothing")


def test_the_order_survives_being_written_down(tmp_path):
    with Store(tmp_path / "rules.db") as store:
        rules = RuleStore(store)
        rules.save(pool(priority=110))
        rules.save(rule("deny-8080", {8080}, action=Action.DENY, priority=50))
        assert [one.name for one in rules.list()] == ["deny-8080", "allow-pool"]


def test_an_older_database_reads_back_with_everything_at_the_default(tmp_path):
    import sqlite3

    where = tmp_path / "rules.db"
    with Store(where) as store:
        RuleStore(store).save(rule("allow-ssh", {22}))
    with sqlite3.connect(where) as db:
        db.execute("ALTER TABLE rules DROP COLUMN priority")

    with Store(where) as store:
        found = RuleStore(store).list()
        assert [one.name for one in found] == ["allow-ssh"]
        assert found[0].priority == 100


# A range is a range, which the builder promised long before it read one.


def test_a_port_range_is_a_rule_over_all_of_them():
    made = catalogue.rule_for("8000-8999", action=Action.ALLOW)
    assert len(made.ports) == 1000
    assert made.name == "allow-8000-8999"


def test_one_port_is_still_one_port():
    assert catalogue.rule_for("8080", action=Action.ALLOW).ports == {8080}


def test_a_range_that_is_not_one_is_refused():
    for said in ("80-70", "0-10", "1-70000"):
        with pytest.raises(ValueError, match="not a port or a range"):
            catalogue.rule_for(said, action=Action.ALLOW)


def test_a_name_is_still_looked_up_in_the_catalogue():
    assert catalogue.rule_for("ssh", action=Action.ALLOW).ports == {22}


def test_doctor_names_a_rule_that_can_never_run(tmp_path, monkeypatch):
    from warden.core.config import Settings
    from warden.core.health import WARN, _firewall

    where = tmp_path / "rules.db"
    monkeypatch.setenv("WARDEN_DATABASE", str(where))
    with Store(where) as store:
        rules = RuleStore(store)
        rules.save(pool(priority=100))
        rules.save(
            rule("deny-8080", {8080}, action=Action.DENY, source="10.1.0.0/16", priority=200)
        )

    said = [
        check.text
        for check in _firewall(Settings(database=where, update_check=False))
        if check.level == WARN
    ]
    assert any("deny-8080 can never run" in one for one in said)
    assert any("--before allow-pool" in one for one in said)


# Through the command, which is where anybody actually decides an order.


@pytest.fixture
def alone(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("WARDEN_DATABASE", str(tmp_path / "rules.db"))


def listed() -> list[str]:
    said = CliRunner().invoke(app, ["firewall", "list", "--json"]).stdout
    return [one["name"] for one in json.loads(said)]


def write(*args: str):
    return CliRunner().invoke(app, ["firewall", *args], catch_exceptions=False)


def test_written_rules_come_back_in_the_order_they_were_written(alone):
    write("allow", "ssh")
    write("allow", "8000-8999", "--from", "10.0.0.0/8")
    assert listed() == ["allow-ssh", "allow-8000-8999"]


def test_before_moves_the_new_rule_in_front_of_a_named_one(alone):
    write("allow", "ssh")
    write("allow", "8000-8999", "--from", "10.0.0.0/8")
    said = write("deny", "8080", "--before", "allow-8000-8999")
    assert said.exit_code == 0
    assert listed() == ["allow-ssh", "deny-8080", "allow-8000-8999"]


def test_after_moves_it_behind_one(alone):
    write("allow", "ssh")
    write("allow", "8000-8999", "--from", "10.0.0.0/8")
    write("deny", "8080", "--after", "allow-ssh")
    assert listed() == ["allow-ssh", "deny-8080", "allow-8000-8999"]


def test_a_placement_against_a_rule_that_is_not_there_writes_nothing(alone):
    write("allow", "ssh")
    said = CliRunner().invoke(app, ["firewall", "deny", "8080", "--before", "allow-nothing"])
    assert said.exit_code == 1
    assert "allow-nothing" in said.stderr
    assert listed() == ["allow-ssh"]

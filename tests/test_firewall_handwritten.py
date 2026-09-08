"""Adopting a ruleset nothing is managing, and being loud about what is lost."""

import json

import pytest
from typer.testing import CliRunner

from warden.cli import app
from warden.firewall import adopt, handwritten
from warden.firewall.model import Action, Direction, Protocol

runner_cli = CliRunner()


def ruleset(*rules: dict, chains: list[dict] | None = None) -> str:
    said = chains or [
        {
            "family": "inet",
            "table": "filter",
            "name": "input",
            "handle": 1,
            "type": "filter",
            "hook": "input",
            "prio": 0,
            "policy": "drop",
        }
    ]
    return json.dumps(
        {
            "nftables": [
                {"metainfo": {"version": "1.0.6"}},
                {"table": {"family": "inet", "name": "filter", "handle": 1}},
                *({"chain": chain} for chain in said),
                *({"rule": rule} for rule in rules),
            ]
        }
    )


def rule(*expr: dict, chain: str = "input", handle: int = 4) -> dict:
    return {
        "family": "inet",
        "table": "filter",
        "chain": chain,
        "handle": handle,
        "expr": list(expr),
    }


def dport(right: object, protocol: str = "tcp") -> dict:
    return {
        "match": {
            "op": "==",
            "left": {"payload": {"protocol": protocol, "field": "dport"}},
            "right": right,
        }
    }


def saddr(right: object) -> dict:
    return {
        "match": {
            "op": "==",
            "left": {"payload": {"protocol": "ip", "field": "saddr"}},
            "right": right,
        }
    }


def read(*rules: dict, listing: str = "", **more) -> adopt.Reading:
    return adopt.from_nftables(ruleset(*rules, **more), listing)


# What warden can hold


def test_a_port_and_a_verdict_is_a_rule():
    found = read(rule(dport(22), {"accept": None}))
    assert found.untranslated == []
    assert len(found.rules) == 1
    said = found.rules[0]
    assert (said.action, said.protocol, said.ports) == (Action.ALLOW, Protocol.TCP, {22})
    assert said.direction is Direction.IN
    assert said.origin.value == "adopted"


def test_a_source_comes_across_as_a_network():
    found = read(rule(dport(22), saddr({"prefix": {"addr": "10.0.0.0", "len": 8}}),
                      {"accept": None}))
    assert found.rules[0].source == "10.0.0.0/8"


def test_a_set_of_ports_is_one_rule_over_all_of_them():
    found = read(rule(dport({"set": [80, 443]}), {"accept": None}))
    assert found.rules[0].ports == {80, 443}


def test_a_range_of_ports_is_the_whole_range():
    found = read(rule(dport({"range": [8000, 8010]}, "udp"), {"accept": None}))
    assert found.rules[0].ports == set(range(8000, 8011))
    assert found.rules[0].protocol is Protocol.UDP


def test_a_counter_is_bookkeeping_rather_than_a_decision():
    found = read(rule(dport(22), {"counter": {"packets": 1, "bytes": 40}}, {"accept": None}))
    assert found.untranslated == []
    assert found.rules[0].ports == {22}


def test_a_rate_comes_across_as_a_rate():
    found = read(rule(dport(22), {"limit": {"rate": 6, "per": "minute"}}, {"accept": None}))
    assert found.rules[0].limit == "6/minute"


def test_an_interface_is_kept_rather_than_widened():
    """A rule that only applies on `lo`, adopted as one that applies everywhere,
    is a door opened by mistranslation."""
    found = read(rule({"match": {"op": "==", "left": {"meta": {"key": "iifname"}},
                                 "right": "lo"}}, {"accept": None}))
    assert found.rules[0].interface == "lo"


def test_every_verdict_has_a_word_in_wardens_own_terms():
    for said, action in (("accept", Action.ALLOW), ("drop", Action.DENY)):
        found = read(rule(dport(22), {said: None}))
        assert found.rules[0].action is action
    found = read(rule(dport(25), {"reject": {"type": "icmp"}}))
    assert found.rules[0].action is Action.REJECT


def test_the_hook_is_which_way_the_traffic_goes():
    chains = [
        {"family": "inet", "table": "filter", "name": "output", "handle": 2,
         "type": "filter", "hook": "output", "prio": 0, "policy": "accept"}
    ]
    found = read(rule(dport(25), {"drop": None}, chain="output"), chains=chains)
    assert found.rules[0].direction is Direction.OUT


# What it will not pretend to hold


def test_connection_tracking_is_named_rather_than_dropped():
    """The first line of nearly every hand-written ruleset, and warden has no word for it."""
    found = read(
        rule({"match": {"op": "==", "left": {"ct": {"key": "state"}},
                        "right": {"set": ["established", "related"]}}}, {"accept": None})
    )
    assert found.rules == []
    assert "ct" in found.untranslated[0]


def test_a_jump_is_named():
    found = read(rule(dport(8080), {"jump": {"target": "web"}}))
    assert "jump" in found.untranslated[0]


def test_a_rule_in_a_chain_nothing_hooks_is_named():
    chains = [
        {"family": "inet", "table": "filter", "name": "input", "handle": 1,
         "type": "filter", "hook": "input", "prio": 0, "policy": "drop"},
        {"family": "inet", "table": "filter", "name": "web", "handle": 3},
    ]
    found = read(rule({"drop": None}, chain="web"), chains=chains)
    assert found.rules == []
    assert "only reached by a jump" in found.untranslated[0]


def test_a_nat_chain_is_not_a_filter():
    chains = [
        {"family": "inet", "table": "filter", "name": "post", "handle": 5,
         "type": "nat", "hook": "postrouting", "prio": 100, "policy": "accept"}
    ]
    found = read(rule({"accept": None}, chain="post"), chains=chains)
    assert "nat chain" in found.untranslated[0]


def test_forwarding_is_not_something_warden_holds():
    chains = [
        {"family": "inet", "table": "filter", "name": "forward", "handle": 3,
         "type": "filter", "hook": "forward", "prio": 0, "policy": "drop"}
    ]
    found = read(rule(dport(22), {"accept": None}, chain="forward"), chains=chains)
    assert "forward" in found.untranslated[0]


def test_a_set_of_addresses_is_not_one_source():
    found = read(rule(dport(22), saddr({"set": ["10.0.0.1", "10.0.0.2"]}), {"accept": None}))
    assert "set of addresses" in found.untranslated[0]


def test_a_rule_that_only_counts_is_not_a_decision():
    found = read(rule({"counter": {"packets": 1, "bytes": 40}}))
    assert "no verdict" in found.untranslated[0]


def test_a_mark_or_anything_else_is_named_by_its_own_word():
    found = read(rule({"mangle": {"key": {"meta": {"key": "mark"}}, "value": 1}}))
    assert "mangle" in found.untranslated[0]


def test_what_could_not_be_read_is_said_in_the_words_it_was_written_in():
    """The person reading the report is the one who wrote the ruleset."""
    listing = "\t\tct state established,related accept # handle 4"
    found = read(
        rule({"match": {"op": "==", "left": {"ct": {"key": "state"}},
                        "right": "established"}}, {"accept": None}),
        listing=listing,
    )
    assert found.untranslated[0].startswith("ct state established,related accept # handle 4")


def test_an_answer_that_is_not_json_is_not_an_empty_ruleset():
    said = adopt.from_nftables("nft: command not found")
    assert said.rules == []
    assert said.untranslated == ["nft did not answer with json"]


# How much of it made it


def test_it_counts_what_was_there_rather_than_what_it_kept():
    found = read(
        rule(dport(22), {"accept": None}, handle=4),
        rule({"match": {"op": "==", "left": {"ct": {"key": "state"}},
                        "right": "established"}}, {"accept": None}, handle=5),
    )
    assert (len(found.rules), len(found.untranslated), found.seen) == (1, 1, 2)
    assert not found.whole
    assert not found.mostly_lost


def test_more_than_half_unreadable_is_not_an_adoption():
    found = read(
        rule(dport(22), {"accept": None}, handle=4),
        rule({"jump": {"target": "web"}}, handle=5),
        rule({"jump": {"target": "web"}}, handle=6),
    )
    assert found.mostly_lost


def test_a_ruleset_that_came_across_whole_says_so():
    found = read(rule(dport(22), {"accept": None}))
    assert found.whole
    assert not found.mostly_lost


def test_counting_the_rules_does_not_mean_reading_them():
    """`managing()` asks whether there is anything here at all, and stops there."""
    assert handwritten.rules_in(ruleset(rule(dport(22), {"accept": None}))) == 1
    assert handwritten.rules_in(ruleset()) == 0
    assert handwritten.rules_in("not json") == 0


# What somebody sees before they decide


@pytest.fixture
def found(monkeypatch, tmp_path) -> adopt.Reading:
    monkeypatch.setenv("WARDEN_DATABASE", str(tmp_path / "rules.db"))
    reading = adopt.from_nftables(
        ruleset(
            rule(dport(22), saddr({"prefix": {"addr": "10.0.0.0", "len": 8}}),
                 {"accept": None}, handle=4),
            rule({"match": {"op": "==", "left": {"ct": {"key": "state"}},
                            "right": "established"}}, {"accept": None}, handle=5),
        ),
        "\t\tct state established accept # handle 5",
    )
    monkeypatch.setattr(adopt, "managing", lambda: [adopt.NFTABLES])
    monkeypatch.setattr(adopt, "read", lambda manager: reading)
    return reading


def test_the_report_says_how_much_of_it_warden_can_hold(found):
    said = runner_cli.invoke(app, ["firewall", "adopt", "--yes"])
    assert "holding 2 rules, 1 of which warden can hold" in said.stdout
    assert "ct state established accept" in said.stderr


def test_a_run_nobody_is_watching_stops_where_rules_would_be_lost(found):
    """`--yes` is for a run nobody is watching, and this is when somebody should be."""
    said = runner_cli.invoke(app, ["firewall", "adopt", "--yes"])
    assert said.exit_code == 1
    assert "--yes will not do it" in said.stderr


def test_saying_no_leaves_the_machine_alone(found):
    said = runner_cli.invoke(app, ["firewall", "adopt"], input="n\n")
    assert said.exit_code == 0
    assert "left alone" in said.stdout


def test_the_question_names_what_would_be_lost(found):
    said = runner_cli.invoke(app, ["firewall", "adopt"], input="n\n")
    assert "losing 1 of 2 rules?" in said.stdout


def test_nothing_here_is_managed_so_nothing_is_turned_off():
    assert adopt.NFTABLES not in adopt.MANAGERS
    assert adopt.stand_down(adopt.NFTABLES) == []

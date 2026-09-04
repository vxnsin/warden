import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from warden.cli import app
from warden.core.store import RuleStore, Store
from warden.errors import WardenError
from warden.firewall import catalogue
from warden.firewall.backends.nftables import Nftables, line
from warden.firewall.model import Action, Direction, Origin, Policy, Protocol, Rule

runner_cli = CliRunner()


def rule(name: str = "ssh", **overrides) -> Rule:
    return Rule(**{"name": name, "ports": {22}, **overrides})


def rendered(*rules: Rule, **policy) -> str:
    return Nftables().render(Policy(rules=list(rules), **policy))


def test_a_name_from_the_catalogue_knows_its_protocol_and_ports():
    assert catalogue.look_up("ssh") == (Protocol.TCP, {22})
    assert catalogue.look_up("HTTPS") == (Protocol.TCP, {443})
    assert catalogue.look_up("dns") == (Protocol.UDP, {53})


def test_a_name_nobody_knows_is_refused_with_a_suggestion():
    with pytest.raises(WardenError, match="did you mean ssh"):
        catalogue.look_up("sshd")


def test_a_port_that_has_a_name_is_shown_by_it():
    assert catalogue.named(Protocol.TCP, {443}) == "https"
    assert catalogue.named(Protocol.TCP, {8000}) is None


def test_an_address_that_is_not_one_is_refused():
    with pytest.raises(ValueError, match="not an address or a network"):
        rule(source="the office")


def test_a_port_outside_the_range_of_ports_is_refused():
    with pytest.raises(ValueError, match="no such port"):
        rule(ports={70000})


def test_a_protocol_without_ports_may_not_name_any():
    with pytest.raises(ValueError, match="has no ports to name"):
        rule(protocol=Protocol.ICMP, ports={22})


def test_a_bare_address_becomes_a_network():
    assert rule(source="10.1.2.3").source == "10.1.2.3/32"


def test_the_ruleset_keeps_the_session_that_applies_it():
    """Without these two lines a default-drop policy drops your own ssh."""
    text = rendered()
    assert "ct state established,related accept" in text
    assert "iif lo accept" in text


def test_the_whole_ruleset_is_replaced_in_one_step():
    assert "flush ruleset" in rendered()


def test_the_default_answer_is_no_for_incoming_and_yes_for_outgoing():
    text = rendered()
    assert "type filter hook input priority filter; policy drop;" in text
    assert "type filter hook output priority filter; policy accept;" in text


def test_a_rule_becomes_a_line_a_person_can_read():
    assert line(rule(source="10.0.0.0/8", comment="office")) == (
        'ip saddr 10.0.0.0/8 tcp dport 22 accept comment "office"'
    )


def test_several_ports_become_a_set():
    assert "tcp dport { 80, 443 }" in line(rule(ports={443, 80}))


def test_the_verdicts_are_the_ones_nftables_uses():
    assert line(rule(action=Action.DENY)).endswith('drop comment "ssh"')
    assert line(rule(action=Action.REJECT)).endswith('reject comment "ssh"')


def test_an_interface_is_named_for_the_direction_it_is_on():
    assert line(rule(interface="eth0")).startswith("iif eth0")
    assert line(rule(direction=Direction.OUT, interface="eth0")).startswith("oif eth0")


def test_icmp_needs_no_ports():
    assert "ip protocol icmp accept" in line(rule(protocol=Protocol.ICMP, ports=set()))


def test_outgoing_rules_go_in_the_output_chain():
    text = rendered(rule(name="out", direction=Direction.OUT, ports={25}))
    output = text.split("chain output")[1]
    assert "tcp dport 25" in output


def test_a_disabled_rule_is_not_in_the_ruleset():
    assert "dport 22" not in rendered(rule(enabled=False))


def test_a_rule_whose_lease_ran_out_is_not_in_the_ruleset():
    """A hole must not outlive the service that borrowed it."""
    gone = rule(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert "dport 22" not in rendered(gone)


def test_a_rule_still_within_its_lease_is():
    live = rule(expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert "dport 22" in rendered(live)


def test_the_same_policy_renders_the_same_bytes_twice():
    policy = Policy(rules=[rule("a"), rule("b", ports={80})])
    assert Nftables().render(policy) == Nftables().render(policy)


def test_rules_survive_a_round_trip_through_the_database():
    with Store(":memory:") as store:
        rules = RuleStore(store)
        kept = rule(
            source="10.0.0.0/8",
            origin=Origin.REGISTRY,
            service="shop-api",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            comment="from the registry",
        )
        rules.save(kept)
        back = rules.get("ssh")
        assert back is not None
        assert (back.source, back.origin, back.service) == (
            "10.0.0.0/8",
            Origin.REGISTRY,
            "shop-api",
        )
        assert back.expires_at == kept.expires_at


def test_rules_can_be_asked_for_by_where_they_came_from():
    with Store(":memory:") as store:
        rules = RuleStore(store)
        rules.save_many([rule("mine"), rule("theirs", origin=Origin.ADOPTED)])
        assert [r.name for r in rules.list(origin="adopted")] == ["theirs"]


def test_saving_a_rule_twice_replaces_rather_than_repeats():
    with Store(":memory:") as store:
        rules = RuleStore(store)
        rules.save(rule(source="10.0.0.0/8"))
        rules.save(rule(source="192.168.0.0/16"))
        assert len(rules.list()) == 1
        assert rules.get("ssh").source == "192.168.0.0/16"


class Away:
    """A rule store on a database no test shares with another."""

    def __init__(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("WARDEN_DATABASE", str(tmp_path / "rules.db"))


@pytest.fixture
def alone(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Away:
    return Away(monkeypatch, tmp_path)


def test_the_command_writes_a_rule_down_without_applying_it(alone: Away):
    result = runner_cli.invoke(
        app, ["firewall", "allow", "ssh", "--from", "10.0.0.0/8"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "not applied" in result.stdout
    listed = json.loads(runner_cli.invoke(app, ["firewall", "list", "--json"]).stdout)
    assert [(r["name"], r["source"], r["origin"]) for r in listed] == [
        ("allow-ssh", "10.0.0.0/8", "catalogue")
    ]


def test_a_bare_port_is_taken_as_one(alone: Away):
    runner_cli.invoke(app, ["firewall", "allow", "8080"])
    listed = json.loads(runner_cli.invoke(app, ["firewall", "list", "--json"]).stdout)
    assert listed[0]["ports"] == [8080]
    assert listed[0]["origin"] == "manual"


def test_a_service_nobody_knows_is_refused_before_anything_is_written(alone: Away):
    result = runner_cli.invoke(app, ["firewall", "allow", "sshd"])
    assert result.exit_code == 1
    assert "did you mean ssh" in result.stderr
    assert runner_cli.invoke(app, ["firewall", "list", "--json"]).stdout.strip() == "[]"


def test_denying_says_nothing_and_rejecting_answers(alone: Away):
    runner_cli.invoke(app, ["firewall", "deny", "23"])
    runner_cli.invoke(app, ["firewall", "deny", "25", "--reject"])
    listed = json.loads(runner_cli.invoke(app, ["firewall", "list", "--json"]).stdout)
    assert sorted(r["action"] for r in listed) == ["deny", "reject"]


def test_a_rule_can_be_taken_away_again(alone: Away):
    runner_cli.invoke(app, ["firewall", "allow", "ssh"])
    removed = runner_cli.invoke(app, ["firewall", "delete", "allow-ssh"])
    assert removed.exit_code == 0
    assert runner_cli.invoke(app, ["firewall", "list", "--json"]).stdout.strip() == "[]"


def test_taking_away_a_rule_that_is_not_there_says_so(alone: Away):
    result = runner_cli.invoke(app, ["firewall", "delete", "nothing"])
    assert result.exit_code == 1
    assert "no rule called" in result.stderr


def test_an_empty_machine_says_so_rather_than_printing_a_bare_table(alone: Away):
    assert "no rules yet" in runner_cli.invoke(app, ["firewall", "list"]).stdout


def test_export_prints_the_ruleset_and_changes_nothing(alone: Away):
    runner_cli.invoke(app, ["firewall", "allow", "ssh", "--from", "10.0.0.0/8"])
    result = runner_cli.invoke(app, ["firewall", "export", "--for", "nftables"])
    assert result.exit_code == 0
    assert "flush ruleset" in result.stdout
    assert "tcp dport 22 accept" in result.stdout


def test_a_ruleset_can_be_written_for_a_firewall_this_machine_does_not_have(alone: Away):
    """Reading it on a laptop before it reaches the machine it is for."""
    runner_cli.invoke(app, ["firewall", "allow", "ssh"])
    written = runner_cli.invoke(app, ["firewall", "export", "--for", "nftables"])
    assert "table inet warden" in written.stdout


def test_a_firewall_nobody_has_heard_of_is_refused_by_name(alone: Away):
    result = runner_cli.invoke(app, ["firewall", "export", "--for", "iptables"])
    assert result.exit_code == 1
    assert "no firewall backend called 'iptables'" in result.stderr

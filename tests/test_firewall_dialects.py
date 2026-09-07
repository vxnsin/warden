"""What each firewall is told, in its own words."""

import pytest

from warden.firewall import backends
from warden.firewall.backends.base import KNOWN, backend_for
from warden.firewall.backends.iptables import Iptables
from warden.firewall.backends.nftables import Nftables
from warden.firewall.backends.pf import Pf
from warden.firewall.backends.windows import Windows
from warden.firewall.model import Action, Direction, Policy, Protocol, Rule

EVERY = (Nftables, Iptables, Pf, Windows)


def rule(**overrides) -> Rule:
    return Rule(**{"name": "ssh", "ports": {22}, "source": "10.0.0.0/8", **overrides})


def policy(*rules: Rule) -> Policy:
    return Policy(rules=list(rules) or [rule()])


@pytest.mark.parametrize("backend", EVERY)
def test_every_dialect_writes_the_port_and_the_network(backend):
    written = backend().render(policy())
    assert "22" in written
    assert "10.0.0.0/8" in written


def test_the_dialects_that_need_it_say_so_about_established_traffic():
    """A default-drop policy that drops your own ssh is a lost machine.

    nftables, iptables and pf all have to be told. Windows Defender Firewall
    filters statefully whatever it is told, so allowing outbound is what lets
    the answers back - there is no line to write.
    """
    assert "ct state established,related accept" in Nftables().render(policy())
    assert "--ctstate ESTABLISHED,RELATED" in Iptables().render(policy())
    assert "keep state" in Pf().render(policy())
    assert "allowoutbound" in Windows().render(policy())


@pytest.mark.parametrize("backend", EVERY)
def test_every_dialect_says_it_was_written_by_warden(backend):
    assert "warden" in backend().render(policy()).lower()


@pytest.mark.parametrize("backend", EVERY)
def test_the_same_policy_renders_the_same_bytes_twice(backend):
    kept = policy(rule(name="a"), rule(name="b", ports={80}))
    assert backend().render(kept) == backend().render(kept)


@pytest.mark.parametrize("backend", EVERY)
def test_a_rule_whose_lease_ran_out_reaches_no_dialect(backend):
    from datetime import UTC, datetime, timedelta

    gone = rule(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert "22" not in backend().render(policy(gone))


def test_iptables_uses_multiport_only_when_there_is_more_than_one():
    from warden.firewall.backends.iptables import line

    assert "--dport 22" in line(rule())
    assert "-m multiport --dports 80,443" in line(rule(ports={80, 443}))
    assert "--dports 8000:8010" in line(rule(ports=set(range(8000, 8011))))


def test_pf_writes_a_list_in_braces_and_a_range_with_a_colon():
    """pf minds the difference, and refuses the file if you do not."""
    from warden.firewall.backends.pf import line

    assert "port { 80, 443 }" in line(rule(ports={80, 443}))
    assert "port 8000:8010" in line(rule(ports=set(range(8000, 8011))))


def test_windows_puts_every_rule_in_one_group_it_can_remove_again():
    from warden.firewall.backends.windows import GROUP, line

    written = line(rule())
    assert f'group="{GROUP}"' in written
    assert "localport=22" in written
    assert "remoteip=10.0.0.0/8" in written


def test_windows_clears_its_own_group_before_writing_it_again():
    written = Windows().render(policy())
    assert 'delete rule group="warden"' in written.splitlines()[1]


def test_the_verdicts_are_the_ones_each_system_uses():
    from warden.firewall.backends.iptables import line as iptables_line
    from warden.firewall.backends.pf import line as pf_line

    assert "-j DROP" in iptables_line(rule(action=Action.DENY))
    assert "-j REJECT" in iptables_line(rule(action=Action.REJECT))
    assert pf_line(rule(action=Action.DENY)).startswith("block drop")
    assert pf_line(rule(action=Action.REJECT)).startswith("block return")


def test_an_outgoing_rule_lands_in_the_outgoing_direction():
    from warden.firewall.backends.iptables import line as iptables_line
    from warden.firewall.backends.pf import line as pf_line
    from warden.firewall.backends.windows import line as windows_line

    out = rule(direction=Direction.OUT, ports={25})
    assert "-A OUTPUT" in iptables_line(out)
    assert pf_line(out).startswith("pass out ")
    assert "dir=out" in windows_line(out)


def test_a_protocol_with_no_ports_says_only_the_protocol():
    from warden.firewall.backends.iptables import line as iptables_line

    icmp = rule(protocol=Protocol.ICMP, ports=set())
    assert "-p icmp" in iptables_line(icmp)
    assert "--dport" not in iptables_line(icmp)


def test_each_system_gets_the_backend_that_belongs_to_it():
    backends.load()
    assert backend_for(system="Linux").kind == "nftables"
    assert backend_for(system="Darwin").kind == "pf"
    assert backend_for(system="Windows").kind == "windows"
    assert backend_for(system="FreeBSD").kind == "pf"
    assert backend_for("iptables").kind == "iptables"


def test_every_backend_in_the_folder_found_itself():
    backends.load()
    assert {"nftables", "iptables", "pf", "windows"} <= set(KNOWN)

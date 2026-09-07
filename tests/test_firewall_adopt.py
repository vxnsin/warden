"""Reading another firewall, which must never quietly lose one of its rules."""

from warden.firewall.adopt import FIREWALLD, UFW, from_firewalld, from_ufw, managing
from warden.firewall.model import Action, Direction, Origin, Protocol

UFW_STATUS = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 8000:8100/tcp              ALLOW IN    10.0.0.0/8
[ 3] 3389                       DENY IN     Anywhere
[ 4] 443/tcp                    ALLOW IN    Anywhere (v6)
[ 5] 25/tcp                     REJECT OUT  Anywhere
"""

FIREWALLD_LISTING = """public (active)
  target: default
  icmp-block-inversion: no
  interfaces: eth0
  sources: 10.0.0.0/8
  services: ssh http
  ports: 8080/tcp 5000-5010/udp
  protocols:
  forward: yes
"""


def named(reading, name):
    return next(rule for rule in reading.rules if rule.name == name)


def test_a_ufw_rule_becomes_a_warden_rule():
    reading = from_ufw(UFW_STATUS)
    ssh = named(reading, "allow-22")
    assert (ssh.action, ssh.protocol, ssh.ports, ssh.source) == (
        Action.ALLOW,
        Protocol.TCP,
        {22},
        "any",
    )
    assert ssh.origin is Origin.ADOPTED
    assert "from ufw" in ssh.comment


def test_a_range_written_the_way_ufw_writes_it():
    rule = named(from_ufw(UFW_STATUS), "allow-8000-8100-10-0-0-0")
    assert min(rule.ports) == 8000
    assert max(rule.ports) == 8100
    assert rule.source == "10.0.0.0/8"


def test_a_bare_port_number_means_both_protocols():
    """ufw means tcp and udp by it, and warden holds one protocol per rule."""
    reading = from_ufw(UFW_STATUS)
    both = {rule.protocol for rule in reading.rules if rule.ports == {3389}}
    assert both == {Protocol.TCP, Protocol.UDP}


def test_the_direction_and_the_verdict_come_across():
    out = named(from_ufw(UFW_STATUS), "reject-25")
    assert out.direction is Direction.OUT
    assert out.action is Action.REJECT


def test_a_line_nobody_can_read_is_named_rather_than_dropped():
    """A rule lost in this step is a door quietly left open, or quietly shut."""
    reading = from_ufw(UFW_STATUS + "[ 6] Anywhere on eth0  ALLOW FWD  10.5.0.0/16\n")
    assert reading.untranslated == ["[ 6] Anywhere on eth0  ALLOW FWD  10.5.0.0/16"]


def test_a_status_with_no_rules_reads_as_nothing_at_all():
    reading = from_ufw("Status: inactive\n")
    assert reading.rules == []
    assert reading.untranslated == []


def test_a_firewalld_service_is_looked_up_in_the_catalogue():
    reading = from_firewalld(FIREWALLD_LISTING)
    assert {22} in [rule.ports for rule in reading.rules]
    assert {80} in [rule.ports for rule in reading.rules]


def test_a_firewalld_zone_source_becomes_the_rule_source():
    reading = from_firewalld(FIREWALLD_LISTING)
    assert {rule.source for rule in reading.rules} == {"10.0.0.0/8"}


def test_a_firewalld_range_is_written_with_a_dash():
    reading = from_firewalld(FIREWALLD_LISTING)
    ranged = next(rule for rule in reading.rules if rule.protocol is Protocol.UDP)
    assert len(ranged.ports) == 11


def test_a_firewalld_service_nobody_knows_is_named_rather_than_dropped():
    reading = from_firewalld("public\n  services: ssh dhcpv6-client\n  ports:\n")
    assert reading.untranslated == ["service dhcpv6-client"]


def test_two_rules_that_would_share_a_name_do_not():
    reading = from_ufw(UFW_STATUS)
    names = [rule.name for rule in reading.rules]
    assert len(names) == len(set(names))


def test_asking_what_manages_this_machine_answers_rather_than_raising():
    """Windows has neither ufw nor firewalld. Saying so beats guessing."""
    found = managing()
    assert isinstance(found, list)
    assert all(name in (UFW, FIREWALLD) for name in found)

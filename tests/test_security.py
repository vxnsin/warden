"""Every way found so far of writing something the generator did not intend.

A firewall that can be talked into a rule, or a proxy config that can be talked
into a backend, is worse than none: it looks like it is holding.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from warden.errors import WardenError
from warden.firewall.adopt import from_ufw
from warden.firewall.backends.iptables import Iptables
from warden.firewall.backends.nftables import Nftables
from warden.firewall.backends.pf import Pf
from warden.firewall.backends.windows import Windows
from warden.firewall.model import Policy, Rule
from warden.models import Registration, RegistrationRequest
from warden.ports import export

BREAKOUTS = [
    'x" accept comment "opened',
    "ok\n\t\ttcp dport 22 accept",
    "a\tb",
    "trailing\r",
    "back" + chr(92) + "slash",
]


@pytest.mark.parametrize("said", BREAKOUTS)
def test_a_comment_cannot_end_the_string_it_is_written_into(said):
    with pytest.raises(ValidationError):
        Rule(name="innocent", ports={8080}, comment=said)


@pytest.mark.parametrize(
    "said", ["eth0 accept; tcp dport 22", "eth0 lo", "eth0\naccept", "a" * 40]
)
def test_an_interface_is_a_name_and_nothing_else(said):
    with pytest.raises(ValidationError):
        Rule(name="innocent", ports={8080}, interface=said)


def test_the_interfaces_a_machine_really_has_are_still_allowed():
    for real in ("eth0", "en0", "wlp3s0", "br-1a2b3c", "veth.100", "lo"):
        assert Rule(name="x", ports={80}, interface=real).interface == real


@pytest.mark.parametrize("backend", [Nftables, Iptables, Pf, Windows])
def test_no_dialect_can_be_given_a_second_rule_through_a_comment(backend):
    """The model refuses first, so the renderers never see it."""
    with pytest.raises(ValidationError):
        rule = Rule(name="x", ports={8080}, comment='a" accept\ntcp dport 22 accept')
        backend().render(Policy(rules=[rule]))


def service(meta: dict[str, str]) -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name="shop-api",
        kind="backend",
        project=None,
        host="127.0.0.1",
        port=8000,
        pid=None,
        meta=meta,
        ttl=None,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )


def test_metadata_cannot_carry_a_line_break_into_a_proxy_config():
    """meta arrives over the API, so it is checked rather than trusted."""
    with pytest.raises(ValidationError):
        RegistrationRequest(
            name="shop-api",
            kind="backend",
            meta={"domain": "x.example.com {\n\treverse_proxy 10.0.0.5:22\n}\nevil.example"},
        )


def test_metadata_cannot_carry_a_quote():
    with pytest.raises(ValidationError):
        RegistrationRequest(name="a", kind="backend", meta={"domain": 'a"b'})


def test_a_one_line_caddy_block_is_still_not_a_hostname():
    """Caddy takes `a.com { reverse_proxy 1.2.3.4:22 }` on one line, so printable
    is not enough - the whole shape has to be a name a resolver would take."""
    with pytest.raises(WardenError, match="is not a hostname"):
        export.hostname(service({"domain": "a.example.com { reverse_proxy 10.0.0.5:22 }"}), None)


def test_a_domain_from_the_command_line_is_checked_the_same_way():
    with pytest.raises(WardenError, match="is not a hostname"):
        export.hostname(service({}), "example.com { reverse_proxy 10.0.0.5:22 }")


def test_the_hostnames_people_actually_use_are_still_written():
    assert export.hostname(service({"domain": "shop.example.com"}), None) == "shop.example.com"
    assert export.hostname(service({}), "example.com") == "shop-api.example.com"
    assert export.hostname(service({}), None) == "shop-api"


def test_nothing_a_proxy_reads_as_syntax_reaches_the_file():
    written = export.render("caddy", [service({"domain": "shop.example.com"})], node="hub")
    assert written.count("reverse_proxy") == 1
    assert "{" in written and written.count("{") == 1


def test_another_firewalls_words_are_tidied_rather_than_trusted():
    """Adoption reads a program's output. It must not refuse the whole run over
    a comment, and it must not carry one through either."""
    awkward = '[ 1] 22/tcp   ALLOW IN    Anywhere  # a"b' + chr(92) + "c"
    reading = from_ufw("Status: active\n" + awkward + "\n")
    assert len(reading.rules) == 1
    comment = reading.rules[0].comment
    assert '"' not in comment
    assert chr(92) not in comment

def test_no_secret_is_printed_by_the_command_that_lists_them(monkeypatch, tmp_path):
    """The most screenshotted command in the tool."""
    from typer.testing import CliRunner

    from warden.cli import app
    from warden.core import config

    monkeypatch.setenv("WARDEN_CONFIG", str(tmp_path / "warden.toml"))
    config.write(
        {
            "token": "the-api-token",
            "cluster_token": "between-wardens",
            "webhook_secret": "the-signing-key",
            "webhook": "https://discord.com/api/webhooks/123456/AbCdEfGh",
        }
    )
    said = CliRunner().invoke(app, ["settings"]).output
    for secret in ("the-api-token", "between-wardens", "the-signing-key", "AbCdEfGh"):
        assert secret not in said, f"{secret} was printed"
    assert "discord.com" in said  # it still says where, just not the whole of it


def test_the_file_holding_them_is_not_readable_by_everyone(monkeypatch, tmp_path):
    import os
    import stat
    import sys

    from warden.core import config

    monkeypatch.setenv("WARDEN_CONFIG", str(tmp_path / "warden.toml"))
    written = config.write({"token": "the-api-token"})
    if sys.platform == "win32":
        return  # the profile's own permissions do this; chmod there is a no-op
    mode = stat.S_IMODE(os.stat(written).st_mode)
    assert not mode & (stat.S_IRGRP | stat.S_IROTH), f"mode is {mode:o}"


def test_the_redaction_does_not_print_credentials_it_was_written_to_hide():
    """It is shown by doctor, by `warden webhook`, by settings, and over the
    API. A netloc carries `user:password@` with it; a hostname does not."""
    from warden.core.events import redacted

    for url, expected in [
        ("https://user:hunter2@hooks.internal/path", "https://hooks.internal/..."),
        ("https://token@hooks.internal:8443/path", "https://hooks.internal:8443/..."),
        ("https://discord.com/api/webhooks/1/secret", "https://discord.com/..."),
        ("http://hooks.example", "http://hooks.example"),
    ]:
        assert redacted(url) == expected
        assert "hunter2" not in (redacted(url) or "")

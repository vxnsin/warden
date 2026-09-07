"""Names a resolver will answer for, and a hosts file that survives being written."""

from datetime import UTC, datetime

import pytest

from warden.errors import WardenError
from warden.models import Registration
from warden.ports import export

NOW = datetime.now(UTC)


def service(name: str, port: int, host: str = "127.0.0.1", **more) -> Registration:
    return Registration(
        name=name,
        kind="backend",
        host=host,
        port=port,
        project=None,
        pid=None,
        meta=more.pop("meta", {}),
        ttl=None,
        created_at=NOW,
        updated_at=NOW,
        expires_at=None,
        **more,
    )


def rendered(*services: Registration, domain: str | None = "test") -> str:
    return export.render("hosts", list(services), node="hub", domain=domain)


def test_a_hosts_block_is_a_name_per_service():
    said = rendered(service("shop-api", 8000), service("docs", 8002))
    assert "127.0.0.1\tdocs.test" in said
    assert "127.0.0.1\tshop-api.test" in said


def test_the_block_carries_its_own_markers_and_its_own_header():
    said = rendered(service("shop-api", 8000))
    assert said.startswith(export.BEGIN)
    assert said.rstrip().endswith(export.END)
    assert "Written by `warden export` from the warden on hub" in said


def test_a_hosts_file_has_no_ports_and_does_not_pretend_to():
    """It gets somebody to the machine; the proxy shapes get them to the service."""
    said = rendered(service("shop-api", 8000))
    assert ":8000" not in said


def test_writing_into_an_empty_file_keeps_nothing_and_adds_the_block():
    said = rendered(service("shop-api", 8000))
    assert export.between_the_markers("", said).strip() == said.strip()


def test_writing_into_somebody_elses_file_leaves_it_alone():
    existing = "127.0.0.1 localhost\n::1 localhost\n10.0.0.5 build-01\n"
    after = export.between_the_markers(existing, rendered(service("shop-api", 8000)))
    for line in existing.splitlines():
        assert line in after


def test_writing_twice_replaces_rather_than_repeats():
    """The whole reason for the markers: a hosts file is not append-only."""
    existing = "127.0.0.1 localhost\n"
    once = export.between_the_markers(existing, rendered(service("shop-api", 8000)))
    twice = export.between_the_markers(once, rendered(service("docs", 8002)))
    assert twice.count(export.BEGIN) == 1
    assert "shop-api.test" not in twice
    assert "docs.test" in twice
    assert "127.0.0.1 localhost" in twice


def test_a_name_that_is_not_a_hostname_is_refused_here_too():
    with pytest.raises(WardenError):
        rendered(service("shop-api", 8000, meta={"domain": "a.com 127.0.0.1 evil"}))


def test_a_service_on_another_node_gets_that_machines_address():
    from warden.models import FleetRegistration, Node

    node = Node(
        name="build-01",
        url="http://10.0.0.5:7010",
        pool_start=9000,
        pool_end=9099,
        version="0.5.1",
        first_seen=NOW,
        last_seen=NOW,
        expires_at=NOW,
    )
    theirs = FleetRegistration(node="build-01", **service("shop-api", 8000).model_dump())
    said = export.render("hosts", [theirs], node="hub", nodes=[node], domain="test")
    assert "10.0.0.5\tshop-api.test" in said


def test_hosts_is_one_of_the_shapes():
    assert export.HOSTS in export.FORMATS


def test_apply_is_refused_for_a_shape_that_has_nowhere_to_go():
    """Where an nginx config belongs is not warden's decision, and never was."""
    from typer.testing import CliRunner

    from warden.cli import app

    result = CliRunner().invoke(app, ["export", "nginx", "--apply"])
    assert result.exit_code == 1
    assert "only for `hosts`" in result.stderr


def test_apply_writes_only_between_the_markers(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from warden.cli import app, shared
    from warden.models import Health

    where = tmp_path / "hosts"
    where.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    monkeypatch.setattr(export, "hosts_file", lambda: where)

    class Pretend:
        url = "http://127.0.0.1:7010"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def health(self) -> Health:
            return Health(status="ok", version="0.5.1", node="hub", role="hub",
                          services=1, nodes=0)

        def nodes(self):
            return []

        def services(self, **_kwargs):
            return [service("shop-api", 8000)]

    monkeypatch.setattr(shared, "_client", lambda *a, **k: Pretend())
    result = CliRunner().invoke(app, ["export", "hosts", "--domain", "test", "--apply"])
    assert result.exit_code == 0, result.stdout

    after = where.read_text(encoding="utf-8")
    assert "127.0.0.1 localhost" in after
    assert "shop-api.test" in after
    assert after.count(export.BEGIN) == 1

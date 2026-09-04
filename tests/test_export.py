from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from warden import __version__, cli
from warden.cli import app
from warden.models import (
    FleetRegistration,
    FleetServices,
    Health,
    Node,
    Registration,
    Unreachable,
)
from warden.ports import export

runner_cli = CliRunner()


def service(name: str, port: int, **kwargs) -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind=kwargs.pop("kind", "backend"),
        project=kwargs.pop("project", None),
        host=kwargs.pop("host", "127.0.0.1"),
        port=port,
        pid=None,
        meta=kwargs.pop("meta", {}),
        ttl=None,
        created_at=now,
        updated_at=now,
        expires_at=None,
        **kwargs,
    )


def on_node(name: str, port: int, node: str, **kwargs) -> FleetRegistration:
    return FleetRegistration(**service(name, port, **kwargs).model_dump(), node=node)


def node(name: str, host: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        name=name,
        url=f"http://{host}:7010",
        pool_start=8000,
        pool_end=8999,
        version=__version__,
        first_seen=now,
        last_seen=now,
        expires_at=now + timedelta(seconds=90),
    )


def rendered(shape: str, services, **kwargs) -> str:
    return export.render(shape, services, node=kwargs.pop("node", "hub"), **kwargs)


def test_caddy_gets_a_site_block_per_service():
    text = rendered(export.CADDY, [service("shop-api", 8000)])
    assert "shop-api {" in text
    assert "\treverse_proxy 127.0.0.1:8000" in text


def test_nginx_gets_a_server_block_that_passes_the_original_host_on():
    text = rendered(export.NGINX, [service("shop-api", 8000)])
    assert "server_name shop-api;" in text
    assert "proxy_pass http://127.0.0.1:8000;" in text
    assert "proxy_set_header Host $host;" in text


def test_traefik_gets_a_router_and_a_service():
    text = rendered(export.TRAEFIK, [service("shop-api", 8000)], domain="example.com")
    assert "rule: Host(`shop-api.example.com`)" in text
    assert "- url: http://127.0.0.1:8000" in text
    assert text.count("    shop-api:") == 2


def test_a_domain_turns_a_name_into_a_hostname():
    text = rendered(export.CADDY, [service("shop-api", 8000)], domain="example.com")
    assert "shop-api.example.com {" in text


def test_a_service_that_carries_its_own_domain_keeps_it():
    carried = service("shop-api", 8000, meta={"domain": "shop.example.com"})
    text = rendered(export.CADDY, [carried], domain="elsewhere.example")
    assert "shop.example.com {" in text
    assert "elsewhere.example" not in text


def test_the_header_says_which_warden_wrote_it():
    text = rendered(export.CADDY, [], node="build-01")
    assert text.splitlines()[0] == (
        "# Written by `warden export` from the warden on build-01. "
        "Regenerate it; do not edit it."
    )


def test_the_same_registry_renders_the_same_bytes_twice():
    """This output belongs in a repository, so it must not churn."""
    services = [service("b", 8001), service("a", 8000)]
    assert rendered(export.CADDY, services) == rendered(export.CADDY, list(reversed(services)))


def test_an_empty_registry_is_a_header_and_nothing_else():
    for shape in export.FORMATS:
        assert rendered(shape, []).strip().startswith("#")
        assert "reverse_proxy" not in rendered(shape, [])


def test_a_service_on_another_machine_is_reached_at_that_machine():
    text = rendered(
        export.CADDY,
        [on_node("shop-api", 8000, "build-01")],
        nodes=[node("build-01", "10.0.0.7")],
    )
    assert "reverse_proxy 10.0.0.7:8000" in text
    assert "127.0.0.1" not in text


def test_a_service_on_a_node_nobody_told_us_about_keeps_its_own_address():
    text = rendered(export.CADDY, [on_node("shop-api", 8000, "build-01")], nodes=[])
    assert "reverse_proxy 127.0.0.1:8000" in text


class FakeClient:
    def __init__(self, services, fleet=None) -> None:
        self.given = services
        self.fleet = fleet
        self.asked: dict[str, object] = {}

    def health(self) -> Health:
        return Health(
            status="ok", version=__version__, node="hub", role="hub", services=1, nodes=0
        )

    def services(self, *, project=None, kind=None):
        self.asked = {"project": project, "kind": kind}
        return self.given

    def fleet_services(self, *, project=None, kind=None) -> FleetServices:
        self.asked = {"project": project, "kind": kind}
        assert self.fleet is not None
        return self.fleet

    def nodes(self):
        return [node("build-01", "10.0.0.7")]

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def only(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient([service("shop-api", 8000, project="shop")])
    monkeypatch.setattr(cli, "_client", lambda url, token: fake)
    return fake


def test_the_command_writes_something_a_proxy_can_read(only: FakeClient):
    result = runner_cli.invoke(app, ["export", "caddy"])
    assert result.exit_code == 0
    assert "reverse_proxy 127.0.0.1:8000" in result.stdout


def test_the_command_passes_the_filters_on(only: FakeClient):
    runner_cli.invoke(app, ["export", "nginx", "--project", "shop", "--kind", "backend"])
    assert only.asked == {"project": "shop", "kind": "backend"}


def test_a_machine_that_could_not_be_asked_is_said_out_loud(monkeypatch: pytest.MonkeyPatch):
    fleet = FleetServices(
        services=[on_node("shop-api", 8000, "build-01")],
        unreachable=[Unreachable(node="db-03", url="http://db-03:7010", reason="timed out")],
    )
    fake = FakeClient([], fleet)
    monkeypatch.setattr(cli, "_client", lambda url, token: fake)
    result = runner_cli.invoke(app, ["export", "caddy", "--all"], catch_exceptions=False)
    assert "reverse_proxy 10.0.0.7:8000" in result.stdout
    assert "db-03" not in result.stdout
    assert "db-03 (http://db-03:7010) timed out" in result.stderr

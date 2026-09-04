import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import DataTable, Static

from warden import theme
from warden.errors import WardenError
from warden.models import (
    FleetListener,
    FleetListeners,
    FleetPool,
    FleetRegistration,
    FleetServices,
    Listener,
    NodePool,
    PoolStatus,
    Registration,
    Unreachable,
)
from warden.tui import (
    BANNER_MIN_HEIGHT,
    COLUMNS,
    PORTS,
    SERVICES,
    WardenApp,
    _address,
    _lease,
)

POOL = PoolStatus(start=8000, end=8004, size=5, reserved=[8004], allocated=2, available=2)


def registration(name: str, port: int, **kwargs) -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind=kwargs.pop("kind", "backend"),
        project=kwargs.pop("project", "shop"),
        host="127.0.0.1",
        port=port,
        pid=kwargs.pop("pid", None),
        meta={},
        ttl=kwargs.pop("ttl", None),
        created_at=now,
        updated_at=now,
        expires_at=kwargs.pop("expires_at", None),
    )


def listener(port: int, **kwargs) -> Listener:
    return Listener(
        protocol=kwargs.pop("protocol", "tcp"),
        host=kwargs.pop("host", "127.0.0.1"),
        port=port,
        pid=kwargs.pop("pid", 4242),
        process=kwargs.pop("process", "python.exe"),
        user=kwargs.pop("user", "WORKSTATION\\dev"),
        started_at=None,
        command=None,
    )


class StubClient:
    url = "http://127.0.0.1:7010"

    def services(self, **_kwargs) -> list[Registration]:
        return []

    def pool(self) -> PoolStatus:
        return POOL

    def listeners(self, **_kwargs) -> list[Listener]:
        return []


def run_app(scenario, size=(120, 40), client=None, fleet=False) -> None:
    async def main() -> None:
        app = WardenApp(client or StubClient(), interval=3600, fleet=fleet)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(main())


def fleet_registration(name: str, port: int, node: str, **kwargs) -> FleetRegistration:
    return FleetRegistration(node=node, **registration(name, port, **kwargs).model_dump())


def fleet_listener(port: int, node: str, **kwargs) -> FleetListener:
    return FleetListener(node=node, **listener(port, **kwargs).model_dump())


def node_pool(node: str, start: int, end: int, allocated: int) -> NodePool:
    return NodePool(
        node=node,
        start=start,
        end=end,
        size=end - start + 1,
        reserved=[],
        allocated=allocated,
        available=(end - start + 1) - allocated,
    )


FLEET_SERVICES = FleetServices(
    services=[
        fleet_registration("runner", 9000, "build-01"),
        fleet_registration("api", 8000, "hub"),
    ],
    unreachable=[
        Unreachable(node="web-02", url="http://web-02:7010", reason="could not be reached")
    ],
)

FLEET_LISTENERS = FleetListeners(
    listeners=[
        fleet_listener(9000, "build-01"),
        fleet_listener(3000, "hub", pid=99, process="node.exe"),
    ],
    unreachable=FLEET_SERVICES.unreachable,
)

FLEET_POOL = FleetPool(
    pools=[node_pool("build-01", 9000, 9099, 1), node_pool("hub", 8000, 8999, 2)],
    unreachable=FLEET_SERVICES.unreachable,
)


class FleetStubClient(StubClient):
    """A hub that answers for two nodes, one of which is not answering."""

    def __init__(self) -> None:
        self.released: list[tuple[str, str | None]] = []
        self.stopped: list[tuple[int, str | None]] = []

    def fleet_services(self, **_kwargs) -> FleetServices:
        return FLEET_SERVICES

    def fleet_listeners(self, **_kwargs) -> FleetListeners:
        return FLEET_LISTENERS

    def fleet_pool(self) -> FleetPool:
        return FLEET_POOL

    def release(self, name: str, *, node: str | None = None) -> None:
        self.released.append((name, node))

    def stop(self, pid: int, *, node: str | None = None, force: bool = False) -> None:
        self.stopped.append((pid, node))


def test_ages_are_shown_in_the_largest_useful_unit():
    now = datetime.now(UTC)
    assert theme.age(now - timedelta(seconds=5)).endswith("s ago")
    assert theme.age(now - timedelta(minutes=5)) == "5m ago"
    assert theme.age(now - timedelta(hours=5)) == "5h ago"
    assert theme.age(now - timedelta(days=5)) == "5d ago"


def test_a_registration_without_a_lease_says_so_quietly():
    lease = _lease(None)
    assert lease.plain == "none"
    assert lease.style == theme.BONE_DIM


def test_a_lapsed_lease_is_marked_in_ember():
    assert _lease(datetime.now(UTC) - timedelta(seconds=1)).style == theme.EMBER


def test_a_lease_about_to_run_out_turns_amber():
    assert _lease(datetime.now(UTC) + timedelta(seconds=30)).style == theme.SHRIEKER


def test_a_healthy_lease_counts_down_in_minutes():
    assert _lease(datetime.now(UTC) + timedelta(minutes=5)).plain == "5m left"


def test_the_port_is_the_lit_part_of_an_address():
    address = _address(registration("api", 8000))
    assert address.plain == "127.0.0.1:8000"
    assert any(span.style == theme.GLOW for span in address.spans)


def test_the_windows_domain_is_stripped_from_an_account():
    assert theme.account("NT AUTHORITY\\SYSTEM") == "SYSTEM"
    assert theme.account("WORKSTATION\\dev") == "dev"
    assert theme.account("dev") == "dev"
    assert theme.account(None) == "-"


def test_every_registration_gets_a_row():
    async def scenario(app: WardenApp, _pilot) -> None:
        app.show_services([registration("api", 8000), registration("web", 8001)], POOL)
        assert app.query_one(DataTable).row_count == 2
        assert [service.name for service in app._services] == ["api", "web"]

    run_app(scenario)


def test_the_stats_line_reports_pool_usage():
    async def scenario(app: WardenApp, _pilot) -> None:
        app.show_services([registration("api", 8000)], POOL)
        stats = str(app.query_one("#stats", Static).content)
        assert "8000-8004" in stats
        assert "2 held" in stats
        assert "2 free" in stats

    run_app(scenario)


def test_an_unreachable_warden_is_shown_instead_of_a_table():
    async def scenario(app: WardenApp, _pilot) -> None:
        app.show_error("no warden reachable")
        stats = app.query_one("#stats", Static)
        assert stats.has_class("-error")
        assert "no warden reachable" in str(stats.content)

    run_app(scenario)


def test_the_banner_is_shown_on_a_tall_terminal():
    async def scenario(app: WardenApp, _pilot) -> None:
        assert app.query_one("#banner", Static).display is True

    run_app(scenario, size=(120, BANNER_MIN_HEIGHT))


def test_the_banner_gives_way_to_the_table_on_a_short_terminal():
    async def scenario(app: WardenApp, _pilot) -> None:
        assert app.query_one("#banner", Static).display is False

    run_app(scenario, size=(120, BANNER_MIN_HEIGHT - 1))


def test_tab_switches_between_the_two_views():
    async def scenario(app: WardenApp, pilot) -> None:
        assert app.view == SERVICES
        assert str(app.query_one("#section", Static).content) == "REGISTERED SERVICES"
        await pilot.press("tab")
        assert app.view == PORTS
        assert str(app.query_one("#section", Static).content) == "LISTENING PORTS"
        await pilot.press("tab")
        assert app.view == SERVICES

    run_app(scenario)


def test_each_view_brings_its_own_columns():
    async def scenario(app: WardenApp, pilot) -> None:
        table = app.query_one(DataTable)
        assert len(table.columns) == len(COLUMNS[SERVICES])
        await pilot.press("tab")
        assert len(table.columns) == len(COLUMNS[PORTS])

    run_app(scenario)


def test_every_socket_gets_a_row_and_wardens_own_are_marked():
    async def scenario(app: WardenApp, pilot) -> None:
        await pilot.press("tab")
        await pilot.pause()
        app.show_ports(
            [listener(8000), listener(3000, pid=99, process="node.exe")],
            [registration("api", 8000)],
        )
        table = app.query_one(DataTable)
        assert table.row_count == 2
        assert [listener.pid for listener in app._listeners] == [4242, 99]
        stats = str(app.query_one("#stats", Static).content)
        assert "2 listening" in stats
        assert "1 handed out by warden" in stats

    run_app(scenario)


def test_sockets_owned_by_another_user_are_counted():
    async def scenario(app: WardenApp, pilot) -> None:
        await pilot.press("tab")
        await pilot.pause()
        app.show_ports([listener(445, pid=None, process=None, user=None)], [])
        assert "1 owned by another user" in str(app.query_one("#stats", Static).content)

    run_app(scenario)


def test_a_socket_without_a_process_cannot_be_stopped():
    async def scenario(app: WardenApp, pilot) -> None:
        await pilot.press("tab")
        await pilot.pause()
        app.show_ports([listener(445, pid=None, process=None, user=None)], [])
        await pilot.press("d")
        stats = app.query_one("#stats", Static)
        assert stats.has_class("-error")
        assert "may not touch" in str(stats.content)

    run_app(scenario)


def test_the_fleet_view_puts_a_node_column_in_both_tables():
    async def scenario(app: WardenApp, pilot) -> None:
        table = app.query_one(DataTable)
        assert len(table.columns) == len(COLUMNS[SERVICES]) + 1
        assert "NODE" in [str(column.label) for column in table.columns.values()]
        await pilot.press("tab")
        await pilot.pause()
        assert "NODE" in [str(column.label) for column in app.query_one(DataTable).columns.values()]

    run_app(scenario, client=FleetStubClient(), fleet=True)


def test_one_warden_alone_has_no_node_column_to_show():
    async def scenario(app: WardenApp, _pilot) -> None:
        assert len(app.query_one(DataTable).columns) == len(COLUMNS[SERVICES])

    run_app(scenario)


def test_a_node_that_did_not_answer_is_named_rather_than_simply_missing():
    async def scenario(app: WardenApp, _pilot) -> None:
        assert "web-02 not answering" in str(app.query_one("#stats", Static).content)

    run_app(scenario, client=FleetStubClient(), fleet=True)


def test_n_steps_the_filter_through_every_node_and_back_to_all():
    async def scenario(app: WardenApp, pilot) -> None:
        assert app.only is None
        assert app.query_one(DataTable).row_count == 2
        await pilot.press("n")
        await pilot.pause()
        assert app.only == "build-01"
        assert [service.name for service in app._services] == ["runner"]
        await pilot.press("n")
        await pilot.pause()
        assert app.only == "hub"
        await pilot.press("n")
        await pilot.pause()
        # A node nobody could reach is still worth singling out and looking at.
        assert app.only == "web-02"
        assert app._services == []
        await pilot.press("n")
        await pilot.pause()
        assert app.only is None

    run_app(scenario, client=FleetStubClient(), fleet=True)


def test_the_filter_is_named_in_the_heading():
    async def scenario(app: WardenApp, pilot) -> None:
        assert "FLEET" in str(app.query_one("#section", Static).content)
        await pilot.press("n")
        await pilot.pause()
        assert "BUILD-01" in str(app.query_one("#section", Static).content)

    run_app(scenario, client=FleetStubClient(), fleet=True)


def test_filtering_to_a_node_shows_that_nodes_own_pool():
    async def scenario(app: WardenApp, pilot) -> None:
        assert "2 wardens" in str(app.query_one("#stats", Static).content)
        await pilot.press("n")
        await pilot.pause()
        assert "9000-9099" in str(app.query_one("#stats", Static).content)

    run_app(scenario, client=FleetStubClient(), fleet=True)


def test_a_node_that_never_reported_a_pool_says_so_instead_of_guessing():
    async def scenario(app: WardenApp, pilot) -> None:
        for _ in range(3):
            await pilot.press("n")
            await pilot.pause()
        assert app.only == "web-02"
        assert "web-02 has not reported a pool" in str(app.query_one("#stats", Static).content)

    run_app(scenario, client=FleetStubClient(), fleet=True)


def test_releasing_in_the_fleet_goes_to_the_node_that_handed_the_port_out():
    async def scenario(app: WardenApp, pilot) -> None:
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.client.released == [("runner", "build-01")]

    run_app(scenario, client=FleetStubClient(), fleet=True)


def test_stopping_in_the_fleet_names_the_machine_the_pid_is_on():
    async def scenario(app: WardenApp, pilot) -> None:
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert "on build-01" in str(app.screen.query_one("#question").content)
        await pilot.press("enter")
        await pilot.pause()
        # A pid means nothing off its own machine, so the node goes with it.
        assert app.client.stopped == [(4242, "build-01")]

    run_app(scenario, client=FleetStubClient(), fleet=True)


class UnreachableClient(StubClient):
    """A warden that is not running."""

    def services(self, **_kwargs):
        raise WardenError("no warden reachable at http://127.0.0.1:7010")

    def pool(self):
        raise WardenError("no warden reachable at http://127.0.0.1:7010")

    def listeners(self, **_kwargs):
        raise WardenError("no warden reachable at http://127.0.0.1:7010")


def run_without_a_warden(scenario, size=(120, 40)) -> None:
    async def main() -> None:
        app = WardenApp(UnreachableClient(), interval=3600)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(main())


@pytest.mark.sockets
def test_the_ports_of_this_machine_show_without_a_warden():
    async def scenario(app: WardenApp, pilot) -> None:
        await pilot.press("tab")
        await pilot.pause()
        assert app.standalone is True
        assert app.query_one(DataTable).row_count > 0

    run_without_a_warden(scenario)


@pytest.mark.sockets
def test_it_says_whose_ports_those_are():
    async def scenario(app: WardenApp, pilot) -> None:
        await pilot.press("tab")
        await pilot.pause()
        assert "no warden running" in str(app.query_one("#tagline", Static).content)

    run_without_a_warden(scenario)


def test_the_services_view_still_needs_a_registry():
    async def scenario(app: WardenApp, _pilot) -> None:
        assert app.query_one("#stats", Static).has_class("-error")

    run_without_a_warden(scenario)


def test_the_warden_it_talks_to_is_named_when_there_is_one():
    async def scenario(app: WardenApp, pilot) -> None:
        await pilot.press("tab")
        await pilot.pause()
        assert app.standalone is False
        assert StubClient.url in str(app.query_one("#tagline", Static).content)

    run_app(scenario)

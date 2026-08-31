import asyncio
from datetime import UTC, datetime, timedelta

from textual.widgets import DataTable, Static

from warden import theme
from warden.models import Listener, PoolStatus, Registration
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


def run_app(scenario, size=(120, 40)) -> None:
    async def main() -> None:
        app = WardenApp(StubClient(), interval=3600)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(main())


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
        assert app._names == ["api", "web"]

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
        assert app._pids == [4242, 99]
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

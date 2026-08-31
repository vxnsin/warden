import asyncio
from datetime import UTC, datetime, timedelta

from textual.widgets import DataTable, Static

from port_manager.models import PoolStatus, Registration
from port_manager.tui import PortManagerApp, _age, _expiry

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
        created_at=now,
        updated_at=now,
        expires_at=kwargs.pop("expires_at", None),
    )


class StubClient:
    url = "http://127.0.0.1:7010"

    def services(self, **_kwargs) -> list[Registration]:
        return []

    def pool(self) -> PoolStatus:
        return POOL


def run_app(scenario) -> None:
    async def main() -> None:
        app = PortManagerApp(StubClient(), interval=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(main())


def test_ages_are_shown_in_the_largest_useful_unit():
    now = datetime.now(UTC)
    assert _age(now - timedelta(seconds=5)).endswith("s ago")
    assert _age(now - timedelta(minutes=5)) == "5m ago"
    assert _age(now - timedelta(hours=5)) == "5h ago"
    assert _age(now - timedelta(days=5)) == "5d ago"


def test_a_registration_without_a_lease_never_expires():
    assert _expiry(None) == "never"


def test_a_lapsed_lease_is_marked_expired():
    assert _expiry(datetime.now(UTC) - timedelta(seconds=1)) == "expired"


def test_a_running_lease_counts_down():
    assert _expiry(datetime.now(UTC) + timedelta(minutes=5)) == "in 5m"


def test_every_registration_gets_a_row():
    async def scenario(app: PortManagerApp, _pilot) -> None:
        app.show([registration("api", 8000), registration("web", 8001)], POOL)
        table = app.query_one(DataTable)
        assert table.row_count == 2
        assert app._names == ["api", "web"]

    run_app(scenario)


def test_the_summary_reports_pool_usage():
    async def scenario(app: PortManagerApp, _pilot) -> None:
        app.show([registration("api", 8000)], POOL)
        summary = app.query_one("#summary", Static)
        assert "2 allocated" in str(summary.content)
        assert "8000-8004" in str(summary.content)

    run_app(scenario)


def test_an_unreachable_registry_is_shown_instead_of_a_table():
    async def scenario(app: PortManagerApp, _pilot) -> None:
        app.show_error("no Port Manager reachable")
        summary = app.query_one("#summary", Static)
        assert summary.has_class("-error")
        assert "no Port Manager reachable" in str(summary.content)

    run_app(scenario)

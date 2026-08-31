from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from port_manager.client import PortManagerClient
from port_manager.errors import PortManagerError
from port_manager.models import PoolStatus, Registration

COLUMNS = ("service", "kind", "project", "address", "pid", "expires", "updated")


def _age(moment: datetime) -> str:
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"


def _expiry(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    seconds = int((moment - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return "expired"
    if seconds < 60:
        return f"in {seconds}s"
    return f"in {math.ceil(seconds / 60)}m"


class Confirm(ModalScreen[bool]):
    """Yes/no question shown over the table."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss(False)", "Cancel", show=False),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question, id="question")
            with Horizontal(id="dialog-actions"):
                yield Button("Release", variant="error", id="confirm")
                yield Button("Cancel", variant="default", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class PortManagerApp(App[None]):
    """Live view of everything the registry has handed out."""

    TITLE = "Port Manager"

    CSS = """
    Screen { background: $surface; }

    #summary {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    #summary.-error { color: $error; }

    DataTable {
        height: 1fr;
        border: round $panel-lighten-2;
    }
    DataTable > .datatable--cursor { background: $accent 40%; }

    #dialog {
        width: 56;
        height: auto;
        padding: 1 2;
        border: round $error;
        background: $panel;
    }
    #dialog-actions { height: auto; padding-top: 1; align-horizontal: right; }
    #dialog-actions Button { margin-left: 1; }
    Confirm { align: center middle; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "refresh", "Refresh"),
        Binding("d", "release", "Release"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client: PortManagerClient, interval: float = 2.0) -> None:
        super().__init__()
        self.client = client
        self.interval = interval
        self.sub_title = client.url
        self._names: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading...", id="summary")
        yield DataTable(id="services", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*(column.upper() for column in COLUMNS))
        self.action_refresh()
        self.set_interval(self.interval, self.action_refresh)

    def action_refresh(self) -> None:
        self.load()

    def action_release(self) -> None:
        table = self.query_one(DataTable)
        if not self._names or table.cursor_row < 0:
            return
        name = self._names[table.cursor_row]
        self.push_screen(
            Confirm(f"Release the port held by '{name}'?"),
            lambda confirmed: self.release(name) if confirmed else None,
        )

    @work(exclusive=True, thread=True, group="load")
    def load(self) -> None:
        try:
            services = self.client.services()
            pool = self.client.pool()
        except PortManagerError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        self.call_from_thread(self.show, services, pool)

    @work(thread=True, group="release")
    def release(self, name: str) -> None:
        try:
            self.client.release(name)
        except PortManagerError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        self.call_from_thread(self.action_refresh)

    def show(self, services: list[Registration], pool: PoolStatus) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self._names = [service.name for service in services]
        for service in services:
            table.add_row(
                service.name,
                service.kind,
                service.project or "-",
                service.address,
                str(service.pid) if service.pid else "-",
                _expiry(service.expires_at),
                _age(service.updated_at),
            )
        if services:
            table.move_cursor(row=min(row, len(services) - 1))

        summary = self.query_one("#summary", Static)
        summary.remove_class("-error")
        summary.update(
            f"pool {pool.start}-{pool.end}  |  {pool.allocated} allocated  |  "
            f"{pool.available} free  |  {len(pool.reserved)} reserved"
        )

    def show_error(self, message: str) -> None:
        summary = self.query_one("#summary", Static)
        summary.add_class("-error")
        summary.update(message)


def run(url: str | None = None, *, token: str | None = None, interval: float = 2.0) -> None:
    with PortManagerClient(url, token=token, timeout=3.0) as client:
        PortManagerApp(client, interval=interval).run()

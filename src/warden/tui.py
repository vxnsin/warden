from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import ClassVar

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Static

from warden import theme
from warden.client import WardenClient
from warden.errors import WardenError
from warden.models import PoolStatus, Registration

COLUMNS = ("#", "SERVICE", "KIND", "PROJECT", "ADDRESS", "PID", "LEASE", "SEEN")

# Below this height the banner would leave the table without room to show anything.
BANNER_MIN_HEIGHT = 26

PALETTE = {
    "sculk": theme.SCULK,
    "raised": theme.SCULK_RAISED,
    "lit": theme.SCULK_LIT,
    "vein": theme.VEIN,
    "vein_bright": theme.VEIN_BRIGHT,
    "bone": theme.BONE,
    "dim": theme.BONE_DIM,
    "glow": theme.GLOW,
    "glow_dim": theme.GLOW_DIM,
    "ember": theme.EMBER,
}

STYLES = """
Screen {
    background: $sculk;
    color: $bone;
}

#shell {
    padding: 1 2;
    height: 1fr;
}

#banner {
    color: $glow;
    height: auto;
}

#tagline {
    color: $dim;
    height: auto;
    padding-bottom: 1;
}

#section {
    color: $dim;
    text-style: bold;
    height: auto;
}

DataTable {
    height: 1fr;
    background: $raised;
    border: round $vein;
    scrollbar-background: $raised;
    scrollbar-color: $vein_bright;
    scrollbar-color-hover: $glow_dim;
}
DataTable:focus { border: round $glow_dim; }
DataTable > .datatable--header { background: $raised; color: $dim; text-style: bold; }
DataTable > .datatable--cursor { background: $lit; }
DataTable > .datatable--hover { background: $lit; }

#stats {
    color: $dim;
    height: auto;
    padding-top: 1;
}
#stats.-error { color: $ember; }

#hints { height: auto; }

Confirm { align: center middle; }
#dialog {
    width: 60;
    height: auto;
    padding: 1 2;
    background: $raised;
    border: round $ember;
}
#dialog-actions { height: auto; padding-top: 1; align-horizontal: right; }
#dialog-actions Button { margin-left: 1; min-width: 12; }
"""


def _age(moment: datetime) -> str:
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"


def _lease(moment: datetime | None) -> Text:
    if moment is None:
        return Text("none", style=theme.BONE_DIM)
    seconds = int((moment - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return Text("expired", style=theme.EMBER)
    if seconds < 60:
        return Text(f"{seconds}s left", style=theme.SHRIEKER)
    return Text(f"{math.ceil(seconds / 60)}m left", style=theme.BONE)


def _address(registration: Registration) -> Text:
    text = Text(f"{registration.host}:", style=theme.BONE_DIM)
    text.append(str(registration.port), style=theme.GLOW)
    return text


def _hints(*pairs: tuple[str, str]) -> Text:
    text = Text()
    for index, (keys, action) in enumerate(pairs):
        if index:
            text.append("   ")
        text.append(keys, style=theme.GLOW)
        text.append(f" {action}", style=theme.BONE_DIM)
    return text


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
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class WardenApp(App[None]):
    """Live view of every port the warden has handed out."""

    TITLE = "warden"
    CSS = STYLES

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("r", "refresh", "Reload"),
        Binding("d", "release", "Release"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client: WardenClient, interval: float = 2.0) -> None:
        super().__init__()
        self.client = client
        self.interval = interval
        self._names: list[str] = []

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **PALETTE}

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Static(theme.BANNER, id="banner")
            yield Static(f"{theme.TAGLINE}  ~  {self.client.url}", id="tagline")
            yield Static("REGISTERED SERVICES", id="section")
            yield DataTable(id="services", cursor_type="row")
            yield Static("", id="stats")
            yield Static(
                _hints(
                    ("up/down/j/k", "move"),
                    ("r", "reload"),
                    ("d", "release"),
                    ("q", "quit"),
                ),
                id="hints",
            )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*COLUMNS)
        table.focus()
        self.action_refresh()
        self.set_interval(self.interval, self.action_refresh)

    def on_resize(self, event: events.Resize) -> None:
        self.query_one("#banner", Static).display = event.size.height >= BANNER_MIN_HEIGHT

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

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
        except WardenError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        self.call_from_thread(self.show, services, pool)

    @work(thread=True, group="release")
    def release(self, name: str) -> None:
        try:
            self.client.release(name)
        except WardenError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        self.call_from_thread(self.action_refresh)

    def show(self, services: list[Registration], pool: PoolStatus) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self._names = [service.name for service in services]
        for index, service in enumerate(services, start=1):
            table.add_row(
                Text(str(index), style=theme.BONE_DIM),
                service.name,
                Text(service.kind, style=theme.kind_colour(service.kind)),
                Text(service.project or "-", style=theme.BONE_DIM),
                _address(service),
                Text(str(service.pid) if service.pid else "-", style=theme.BONE_DIM),
                _lease(service.expires_at),
                Text(_age(service.updated_at), style=theme.BONE_DIM),
            )
        if services:
            table.move_cursor(row=min(row, len(services) - 1))

        stats = Text()
        stats.append(f"pool {pool.start}-{pool.end}", style=theme.BONE_DIM)
        stats.append("  ~  ", style=theme.VEIN_BRIGHT)
        stats.append(str(pool.allocated), style=theme.GLOW)
        stats.append(" held", style=theme.BONE_DIM)
        stats.append("  ~  ", style=theme.VEIN_BRIGHT)
        stats.append(str(pool.available), style=theme.MOSS)
        stats.append(" free", style=theme.BONE_DIM)
        stats.append("  ~  ", style=theme.VEIN_BRIGHT)
        stats.append(f"{len(pool.reserved)} reserved", style=theme.BONE_DIM)

        widget = self.query_one("#stats", Static)
        widget.remove_class("-error")
        widget.update(stats)

    def show_error(self, message: str) -> None:
        widget = self.query_one("#stats", Static)
        widget.add_class("-error")
        widget.update(message)


def run(url: str | None = None, *, token: str | None = None, interval: float = 2.0) -> None:
    with WardenClient(url, token=token, timeout=3.0) as client:
        WardenApp(client, interval=interval).run()

from __future__ import annotations

import math
from collections.abc import Callable
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
from warden.models import Listener, PoolStatus, Registration

SERVICES = "services"
PORTS = "ports"

COLUMNS = {
    SERVICES: ("#", "SERVICE", "KIND", "PROJECT", "ADDRESS", "PID", "LEASE", "SEEN"),
    PORTS: ("#", "PORT", "PROTO", "PROCESS", "PID", "USER", "ADDRESS", "WARDEN"),
}
HEADINGS = {SERVICES: "REGISTERED SERVICES", PORTS: "LISTENING PORTS"}

# Below this height the banner would leave the table without room to show anything.
BANNER_MIN_HEIGHT = 30

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

#banner { height: auto; }

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



def _dim(value: object) -> Text:
    return Text(str(value) if value else "-", style=theme.BONE_DIM)


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

    def __init__(self, question: str, verb: str) -> None:
        super().__init__()
        self.question = question
        self.verb = verb

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question, id="question")
            with Horizontal(id="dialog-actions"):
                yield Button(self.verb, variant="error", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class WardenApp(App[None]):
    """Everything the warden handed out, and everything else that is listening."""

    TITLE = "warden"
    CSS = STYLES

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        # Textual gives tab to focus movement by default; there is only one
        # focusable widget here, so the view switch is the better use for it.
        Binding("tab", "switch", "Switch view", priority=True),
        Binding("r", "refresh", "Reload"),
        Binding("d", "act", "Release/stop"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client: WardenClient, interval: float = 2.0) -> None:
        super().__init__()
        self.client = client
        self.interval = interval
        self.view = SERVICES
        self._names: list[str] = []
        self._pids: list[int | None] = []

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **PALETTE}

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Static(theme.banner_text(), id="banner")
            yield Static(f"{theme.TAGLINE}  ~  {self.client.url}", id="tagline")
            yield Static(HEADINGS[SERVICES], id="section")
            yield DataTable(id="rows", cursor_type="row")
            yield Static("", id="stats")
            yield Static("", id="hints")

    def on_mount(self) -> None:
        self.query_one(DataTable).focus()
        self._lay_out()
        self.action_refresh()
        self.set_interval(self.interval, self.action_refresh)

    def on_resize(self, event: events.Resize) -> None:
        self.query_one("#banner", Static).display = event.size.height >= BANNER_MIN_HEIGHT

    def _lay_out(self) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns(*COLUMNS[self.view])
        self.query_one("#section", Static).update(HEADINGS[self.view])
        self.query_one("#hints", Static).update(
            _hints(
                ("up/down/j/k", "move"),
                ("tab", "ports" if self.view == SERVICES else "services"),
                ("r", "reload"),
                ("d", "release" if self.view == SERVICES else "stop"),
                ("q", "quit"),
            )
        )

    def action_switch(self) -> None:
        self.view = PORTS if self.view == SERVICES else SERVICES
        self._names = []
        self._pids = []
        self._lay_out()
        self.action_refresh()

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_refresh(self) -> None:
        self.load()

    def action_act(self) -> None:
        row = self.query_one(DataTable).cursor_row
        if row < 0:
            return
        if self.view == SERVICES:
            if row >= len(self._names):
                return
            name = self._names[row]
            self.push_screen(
                Confirm(f"Release the port held by '{name}'?", "Release"),
                lambda confirmed: self.release(name) if confirmed else None,
            )
            return
        if row >= len(self._pids):
            return
        pid = self._pids[row]
        if pid is None:
            self.show_error("that socket belongs to a process this user may not touch")
            return
        self.push_screen(
            Confirm(f"Stop process {pid}?", "Stop"),
            lambda confirmed: self.stop(pid) if confirmed else None,
        )

    @work(exclusive=True, thread=True, group="load")
    def load(self) -> None:
        try:
            if self.view == SERVICES:
                self.call_from_thread(
                    self.show_services, self.client.services(), self.client.pool()
                )
            else:
                self.call_from_thread(
                    self.show_ports, self.client.listeners(), self.client.services()
                )
        except WardenError as exc:
            self.call_from_thread(self.show_error, str(exc))

    @work(thread=True, group="act")
    def release(self, name: str) -> None:
        self._act(lambda: self.client.release(name))

    @work(thread=True, group="act")
    def stop(self, pid: int) -> None:
        self._act(lambda: self.client.stop(pid))

    def _act(self, action: Callable[[], None]) -> None:
        try:
            action()
        except WardenError as exc:
            self.call_from_thread(self.show_error, str(exc))
            return
        self.call_from_thread(self.action_refresh)

    def _restore_cursor(self, table: DataTable, row: int, count: int) -> None:
        if count:
            table.move_cursor(row=min(max(row, 0), count - 1))

    def show_services(self, services: list[Registration], pool: PoolStatus) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self._names = [service.name for service in services]
        for index, service in enumerate(services, start=1):
            table.add_row(
                _dim(index),
                service.name,
                Text(service.kind, style=theme.kind_colour(service.kind)),
                _dim(service.project),
                _address(service),
                _dim(service.pid),
                _lease(service.expires_at),
                _dim(theme.age(service.updated_at)),
            )
        self._restore_cursor(table, row, len(services))

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
        self._say(stats)

    def show_ports(self, rows: list[Listener], services: list[Registration]) -> None:
        known = {(service.host, service.port): service.name for service in services}
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self._pids = [listener.pid for listener in rows]
        for index, listener in enumerate(rows, start=1):
            table.add_row(
                _dim(index),
                Text(str(listener.port), style=theme.GLOW),
                _dim(listener.protocol),
                listener.process or Text("unknown", style=theme.BONE_DIM),
                _dim(listener.pid),
                _dim(theme.account(listener.user)),
                _dim(listener.host),
                Text(known.get((listener.host, listener.port), "-"), style=theme.MOSS),
            )
        self._restore_cursor(table, row, len(rows))

        ours = sum(1 for listener in rows if (listener.host, listener.port) in known)
        hidden = sum(1 for listener in rows if listener.pid is None)
        stats = Text()
        stats.append(f"{len(rows)} listening", style=theme.BONE_DIM)
        stats.append("  ~  ", style=theme.VEIN_BRIGHT)
        stats.append(str(ours), style=theme.MOSS)
        stats.append(" handed out by warden", style=theme.BONE_DIM)
        if hidden:
            stats.append("  ~  ", style=theme.VEIN_BRIGHT)
            stats.append(f"{hidden} owned by another user", style=theme.SHRIEKER)
        self._say(stats)

    def _say(self, message: Text | str) -> None:
        stats = self.query_one("#stats", Static)
        stats.remove_class("-error")
        stats.update(message)

    def show_error(self, message: str) -> None:
        stats = self.query_one("#stats", Static)
        stats.add_class("-error")
        stats.update(message)


def run(url: str | None = None, *, token: str | None = None, interval: float = 2.0) -> None:
    with WardenClient(url, token=token, timeout=3.0) as client:
        WardenApp(client, interval=interval).run()

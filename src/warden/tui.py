from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import ClassVar

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static, Tab, Tabs

from warden import theme
from warden.client import WardenClient
from warden.errors import WardenError
from warden.firewall import catalogue
from warden.models import FleetPool, Listener, Node, PoolStatus, Registration, Unreachable
from warden.ports.listeners import listeners
from warden.ports.listeners import stop as stop_here

SERVICES = "services"
PORTS = "ports"
RULES = "rules"
NODES = "nodes"

# `tab` steps through them in this order and comes back round.
VIEWS = (SERVICES, PORTS, RULES, NODES)

COLUMNS = {
    SERVICES: ("#", "SERVICE", "KIND", "PROJECT", "ADDRESS", "PID", "LEASE", "SEEN"),
    PORTS: ("#", "PORT", "PROTO", "PROCESS", "PID", "USER", "ADDRESS", "WARDEN"),
    RULES: ("#", "RULE", "DIR", "ACTION", "WHAT", "FROM", "ORIGIN", "UNTIL"),
    NODES: ("#", "NODE", "ADDRESS", "VERSION", "POOL", "STATUS", "SEEN"),
}
# What the tab bar calls each view. The bar is the heading.
TABS = {SERVICES: "Services", PORTS: "Ports", RULES: "Firewall", NODES: "Nodes"}

SEP = "  ~  "

# What `d` does in each view. Nothing, where a rule is not something to take
# away from under a cursor: closing one is `warden firewall delete`, and it is
# a decision with a ruleset behind it rather than a keypress.
ACTS = {SERVICES: "release", PORTS: "stop", RULES: "close", NODES: "forget"}

# What `a` adds, where there is something to add. A node is not on this list:
# a warden announces itself, and one typed in here would be a name with nothing
# behind it.
ADDS = {SERVICES: "register", RULES: "rule"}

# The same colours the command line gives them.
RULE_ACTIONS = {"allow": theme.MOSS, "deny": theme.EMBER, "reject": theme.SHRIEKER}
NODE_STATES = {"online": theme.MOSS, "stale": theme.SHRIEKER}

# Below this height the banner would leave the table without room to show anything.
BANNER_MIN_HEIGHT = 30

# A hub asking its nodes waits up to aggregate.TIMEOUT for the slowest of them,
# so a dashboard that gave up sooner would show a fleet permanently on fire.
FLEET_TIMEOUT = 8.0

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

/* The same bar the setup screen has, so the two screens look like one program. */
Tabs { background: $sculk; height: 2; }
Tabs > #tabs-scroll { height: 2; }
Tabs > #tabs-list { min-height: 1; }
Tab { color: $dim; padding: 0 2; }
Tab.-active { color: $glow; text-style: bold; }
Underline > .underline--bar { color: $glow_dim; background: $vein; }

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
.ask-field { height: auto; padding-top: 1; }
.ask-name { width: 22; padding-top: 1; color: $dim; }
#dialog Input { width: 1fr; background: $sculk; border: tall $vein; }
#dialog Input:focus { border: tall $glow_dim; }
"""


def _lease(moment: datetime | None) -> Text:
    if moment is None:
        return Text("none", style=theme.BONE_DIM)
    seconds = int((moment - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return Text("expired", style=theme.EMBER)
    style = theme.SHRIEKER if seconds < 60 else theme.BONE
    return Text(f"{theme.until(moment)} left", style=style)


def _address(registration: Registration) -> Text:
    text = Text(f"{registration.host}:", style=theme.BONE_DIM)
    text.append(str(registration.port), style=theme.GLOW)
    return text



def _dim(value: object) -> Text:
    return Text(str(value) if value else "-", style=theme.BONE_DIM)


def _node_name(row: object) -> str:
    """Which warden a row is from, whether it arrived as a model or as fields."""
    if isinstance(row, Mapping):
        return str(row.get("node", "") or "")
    return str(getattr(row, "node", "") or "")


def _expires(said: object) -> Text:
    """When a rule closes itself, in the time left rather than a timestamp."""
    if not said:
        return Text("-", style=theme.BONE_DIM)
    with suppress(ValueError):
        return Text(theme.until(datetime.fromisoformat(str(said))), style=theme.SHRIEKER)
    return Text(str(said))


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


class Ask(ModalScreen[dict[str, str] | None]):
    """A few fields and two buttons, over the table.

    Handing back a plain mapping rather than a typed thing: what the fields
    mean is the caller's business, and every value on the way in is a string
    somebody typed anyway.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
    ]

    def __init__(self, title: str, verb: str, fields: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self.title_said = title
        self.verb = verb
        # name, label, placeholder
        self.fields = fields

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_said, id="question")
            for name, label, placeholder in self.fields:
                with Horizontal(classes="ask-field"):
                    yield Label(label, classes="ask-name")
                    yield Input(placeholder=placeholder, id=f"ask-{name}")
            with Horizontal(id="dialog-actions"):
                yield Button(self.verb, variant="primary", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self.fields:
            self.query_one(f"#ask-{self.fields[0][0]}", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._done()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._done()
            return
        self.dismiss(None)

    def _done(self) -> None:
        said = {
            name: self.query_one(f"#ask-{name}", Input).value.strip()
            for name, _, _ in self.fields
        }
        if not said[self.fields[0][0]]:
            self.query_one(f"#ask-{self.fields[0][0]}", Input).focus()
            return
        self.dismiss(said)


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
        Binding("ctrl+right", "switch", "Next view", show=False, priority=True),
        Binding("ctrl+left", "back", "Previous view", show=False, priority=True),
        Binding("n", "node", "Filter by node"),
        Binding("r", "refresh", "Reload"),
        Binding("a", "add", "Add"),
        Binding("d", "act", "Release/stop"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self, client: WardenClient, interval: float = 2.0, *, fleet: bool = False
    ) -> None:
        super().__init__()
        self.client = client
        self.interval = interval
        self.fleet = fleet
        self.view = SERVICES
        # None means every node. Cycling includes the ones that did not answer,
        # so a machine that has gone quiet can still be singled out and seen.
        self.only: str | None = None
        self._services: list[Registration] = []
        self._listeners: list[Listener] = []
        # Left as they arrived rather than parsed: a hub shows a fleet that may
        # be running a newer warden than itself.
        self._rules: list[dict[str, object]] = []
        self._known: list[Node] = []
        # Not _nodes: that name belongs to Textual, for the widgets on screen.
        self._node_names: list[str] = []
        self._unreachable: list[Unreachable] = []
        self.standalone = False

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **PALETTE}

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Static(theme.banner_text(), id="banner")
            yield Static("", id="tagline")
            yield Tabs(*(Tab(TABS[view], id=f"view-{view}") for view in VIEWS), id="views")
            yield Static("", id="section")
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

    def _columns(self) -> tuple[str, ...]:
        """The view's columns, with NODE behind the number when there is a fleet."""
        first, *rest = COLUMNS[self.view]
        if self.view == NODES or not self.fleet:
            return (first, *rest)
        return (first, "NODE", *rest)

    def _heading(self) -> str:
        """What the tab bar does not already say: whose machines these are.

        The bar names the view, so repeating it underneath would be the same
        word twice with nothing between them.
        """
        return (self.only or "fleet").upper() if self.fleet else ""

    def _lay_out(self) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns(*self._columns())
        self._label()

    def _label(self) -> None:
        """The heading, whose machine this is, and the key hints."""
        source = "this machine, no warden running" if self.standalone else self.client.url
        said = Text(f"{theme.TAGLINE}{SEP}{source}{SEP}", style=theme.BONE_DIM)
        said.append_text(theme.byline())
        self.query_one("#tagline", Static).update(said)
        heading = self._heading()
        section = self.query_one("#section", Static)
        section.update(heading)
        # An empty heading is a blank row rather than nothing, and on a small
        # terminal that row is a row of the table.
        section.display = bool(heading)
        hints = [
            ("up/down/j/k", "move"),
            ("tab", VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]),
            ("r", "reload"),
            ("q", "quit"),
        ]
        if self.view in ADDS:
            hints.insert(3, ("a", ADDS[self.view]))
        if ACTS[self.view]:
            hints.insert(4 if self.view in ADDS else 3, ("d", ACTS[self.view]))
        if self.fleet:
            hints.insert(2, ("n", self.only or "every node"))
        self.query_one("#hints", Static).update(_hints(*hints))

    def action_switch(self) -> None:
        self._step(1)

    def action_back(self) -> None:
        self._step(-1)

    def _step(self, by: int) -> None:
        """The bar is what holds the view; moving it is what changes it."""
        self.query_one(Tabs).active = f"view-{VIEWS[(VIEWS.index(self.view) + by) % len(VIEWS)]}"

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        chosen = str(event.tab.id).removeprefix("view-")
        if chosen == self.view:
            return
        self.view = chosen
        self._services = []
        self._listeners = []
        self._rules = []
        self._known = []
        self._lay_out()
        self.action_refresh()

    def action_node(self) -> None:
        """Step the filter on: everything, then one node at a time."""
        if not self.fleet or not self._node_names:
            return
        order: list[str | None] = [None, *self._node_names]
        step = order.index(self.only) + 1 if self.only in order else 0
        self.only = order[step % len(order)]
        self._label()
        self.action_refresh()

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_refresh(self) -> None:
        self.load()

    def _node_of(self, row: object) -> str | None:
        """Which warden a row belongs to, or None when there is only this one."""
        return getattr(row, "node", None) if self.fleet else None

    def action_add(self) -> None:
        """Register a service, or write a rule down, in the view you are in."""
        if self.view == SERVICES:
            self.push_screen(self._ask_for_a_service(), self._register_said)
        elif self.view == RULES:
            self.push_screen(self._ask_for_a_rule(), self._write_said)
        else:
            self.show_error(
                "nothing to add here - a socket is opened by a process, and a "
                "warden announces itself"
            )

    def _ask_for_a_service(self) -> Ask:
        fields = [
            ("name", "Name", "shop-api"),
            ("kind", "Kind", "backend"),
            ("project", "Project", "optional"),
            ("port", "Port", "any free one"),
        ]
        if self.fleet:
            fields.append(("node", "On node", self.only or "this one"))
        return Ask("Register a service", "Register", fields)

    def _ask_for_a_rule(self) -> Ask:
        fields = [
            ("what", "Port or name", "ssh, 8080, 9000-9010"),
            ("action", "Action", "allow, deny, reject"),
            ("source", "From", "any, 10.0.0.0/8"),
        ]
        if self.fleet:
            fields.append(("node", "On node", self.only or "this one"))
        return Ask("Write a rule down", "Write", fields)

    def _register_said(self, said: dict[str, str] | None) -> None:
        if not said:
            return
        port = said.get("port") or ""
        if port and not port.isdigit():
            self.show_error(f"{port!r} is not a port number")
            return
        self.add_service(
            said["name"],
            kind=said.get("kind") or "service",
            project=said.get("project") or None,
            preferred_port=int(port) if port else None,
            node=said.get("node") or self.only,
        )

    def _write_said(self, said: dict[str, str] | None) -> None:
        if not said:
            return
        self.add_rule(
            said["what"],
            action=said.get("action") or "allow",
            source=said.get("source") or "any",
            node=said.get("node") or self.only,
        )

    @work(thread=True, group="act")
    def add_service(self, name: str, **kwargs: object) -> None:
        self._act(lambda: self.client.register(name, **kwargs))

    @work(thread=True, group="act")
    def add_rule(self, what: str, **kwargs: object) -> None:
        self._act(lambda: self.client.firewall_write(what, **kwargs))

    def action_act(self) -> None:
        row = self.query_one(DataTable).cursor_row
        if row < 0:
            return
        if self.view == SERVICES:
            self._release_row(row)
        elif self.view == PORTS:
            self._stop_row(row)
        elif self.view == RULES:
            self._close_row(row)
        else:
            self._forget_row(row)

    def _release_row(self, row: int) -> None:
        if row >= len(self._services):
            return
        service = self._services[row]
        node = self._node_of(service)
        where = f"{node}/{service.name}" if node else service.name
        self.push_screen(
            Confirm(f"Release the port held by '{where}'?", "Release"),
            lambda confirmed: self.release(service.name, node) if confirmed else None,
        )

    def _stop_row(self, row: int) -> None:
        if row >= len(self._listeners):
            return
        listener = self._listeners[row]
        if listener.pid is None:
            self.show_error("that socket belongs to a process this user may not touch")
            return
        # By node, never by number alone: the same pid on another machine is
        # another process entirely, and this is the key that stops things.
        node = self._node_of(listener)
        pid = listener.pid
        where = f"{pid} on {node}" if node else str(pid)
        self.push_screen(
            Confirm(f"Stop process {where}?", "Stop"),
            lambda confirmed: self.stop(pid, node) if confirmed else None,
        )

    def _close_row(self, row: int) -> None:
        if row >= len(self._rules):
            return
        rule = self._rules[row]
        name = str(rule.get("name", ""))
        node = _node_name(rule) if self.fleet else None
        where = f"{node}/{name}" if node else name
        self.push_screen(
            Confirm(
                f"Close rule '{where}'? It stays in the kernel until the next apply.",
                "Close",
            ),
            lambda confirmed: self.close_rule(name, node) if confirmed else None,
        )

    def _forget_row(self, row: int) -> None:
        if row >= len(self._known):
            return
        node = self._known[row]
        self.push_screen(
            Confirm(
                f"Forget '{node.name}'? It comes back on its own if it is still reporting.",
                "Forget",
            ),
            lambda confirmed: self.forget_node(node.name) if confirmed else None,
        )

    def _load_services(self) -> None:
        if not self.fleet:
            self.call_from_thread(
                self.show_services, self.client.services(), self.client.pool()
            )
            return
        found = self.client.fleet_services()
        self.call_from_thread(
            self.show_services, found.services, self.client.fleet_pool(), found.unreachable
        )

    def _load_ports(self) -> None:
        if not self.fleet:
            try:
                found, services = self.client.listeners(), self.client.services()
                self.standalone = False
            except WardenError:
                # The sockets are this machine's own; only the WARDEN column needs a registry.
                found, services = listeners(), []
                self.standalone = True
            self.call_from_thread(self.show_ports, found, services)
            return
        found = self.client.fleet_listeners()
        self.call_from_thread(
            self.show_ports,
            found.listeners,
            self.client.fleet_services().services,
            found.unreachable,
        )

    @work(exclusive=True, thread=True, group="load")
    def load(self) -> None:
        try:
            if self.view == SERVICES:
                self._load_services()
            elif self.view == PORTS:
                self._load_ports()
            elif self.view == RULES:
                self._load_rules()
            else:
                self._load_nodes()
        except WardenError as exc:
            self.call_from_thread(self.show_error, str(exc))

    @work(thread=True, group="act")
    def release(self, name: str, node: str | None = None) -> None:
        self._act(lambda: self.client.release(name, node=node))

    @work(thread=True, group="act")
    def stop(self, pid: int, node: str | None = None) -> None:
        if self.standalone:
            self._act(lambda: stop_here(pid))
            return
        self._act(lambda: self.client.stop(pid, node=node))

    @work(thread=True, group="act")
    def close_rule(self, name: str, node: str | None = None) -> None:
        self._act(
            lambda: self.client.firewall_close_on(node, name)
            if node
            else self.client.firewall_close(name)
        )

    @work(thread=True, group="act")
    def forget_node(self, name: str) -> None:
        self._act(lambda: self.client.forget(name))

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

    def _remember(self, rows: list, unreachable: list[Unreachable]) -> None:
        """What there is to cycle through, taken from the unfiltered answer.

        Read after filtering, the list would shrink to whatever is showing and
        there would be no way back out of a node.
        """
        self._unreachable = list(unreachable)
        if not self.fleet:
            return
        seen = {_node_name(row) for row in rows} | {u.node for u in unreachable}
        self._node_names = sorted(name for name in seen if name)

    def _showing(self, rows: list) -> list:
        if not self.only:
            return rows
        return [row for row in rows if _node_name(row) == self.only]

    def _cells(self, index: int, node: str, *rest: object) -> tuple[object, ...]:
        head = (_dim(index), node) if self.fleet else (_dim(index),)
        return (*head, *rest)

    def _missing(self, stats: Text) -> Text:
        """Name the nodes that did not answer, rather than quietly showing less."""
        if not self._unreachable:
            return stats
        names = theme.listed([node.node for node in self._unreachable])
        stats.append(SEP, style=theme.VEIN_BRIGHT)
        stats.append(f"{names} not answering", style=theme.SHRIEKER)
        return stats

    def _pool_stats(self, pool: PoolStatus | FleetPool) -> Text:
        """Where the ports stand, for the fleet or for the node being filtered to."""
        stats = Text()
        showing: PoolStatus | FleetPool = pool
        if isinstance(pool, FleetPool) and self.only:
            here = next((one for one in pool.pools if one.node == self.only), None)
            if here is None:
                stats.append(f"{self.only} has not reported a pool", style=theme.BONE_DIM)
                return self._missing(stats)
            showing = here

        if isinstance(showing, FleetPool):
            stats.append(f"{len(showing.pools)} wardens", style=theme.BONE_DIM)
            reserved = None
        else:
            stats.append(f"pool {showing.start}-{showing.end}", style=theme.BONE_DIM)
            reserved = len(showing.reserved)

        stats.append(SEP, style=theme.VEIN_BRIGHT)
        stats.append(str(showing.allocated), style=theme.GLOW)
        stats.append(" held", style=theme.BONE_DIM)
        stats.append(SEP, style=theme.VEIN_BRIGHT)
        stats.append(str(showing.available), style=theme.MOSS)
        stats.append(" free", style=theme.BONE_DIM)
        if reserved is not None:
            stats.append(SEP, style=theme.VEIN_BRIGHT)
            stats.append(f"{reserved} reserved", style=theme.BONE_DIM)
        return self._missing(stats)

    def show_services(
        self,
        services: list[Registration],
        pool: PoolStatus | FleetPool,
        unreachable: list[Unreachable] = (),
    ) -> None:
        self._remember(services, unreachable)
        services = self._showing(services)
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self._services = services
        for index, service in enumerate(services, start=1):
            table.add_row(
                *self._cells(
                    index,
                    getattr(service, "node", ""),
                    service.name,
                    Text(service.kind, style=theme.kind_colour(service.kind)),
                    _dim(service.project),
                    _address(service),
                    _dim(service.pid),
                    _lease(service.expires_at),
                    _dim(theme.age(service.updated_at)),
                )
            )
        self._restore_cursor(table, row, len(services))
        self._say(self._pool_stats(pool))

    def show_ports(
        self,
        rows: list[Listener],
        services: list[Registration],
        unreachable: list[Unreachable] = (),
    ) -> None:
        # Keyed by node as well: 3000 on two machines is two processes, and
        # naming one after the other's service would simply be wrong.
        known = {
            (getattr(service, "node", ""), service.host, service.port): service.name
            for service in services
        }
        self._remember(rows, unreachable)
        rows = self._showing(rows)
        self._label()
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self._listeners = rows
        for index, listener in enumerate(rows, start=1):
            node = getattr(listener, "node", "")
            table.add_row(
                *self._cells(
                    index,
                    node,
                    Text(str(listener.port), style=theme.GLOW),
                    _dim(listener.protocol),
                    listener.process or Text("unknown", style=theme.BONE_DIM),
                    _dim(listener.pid),
                    _dim(theme.account(listener.user)),
                    _dim(listener.host),
                    Text(
                        known.get((node, listener.host, listener.port), "-"), style=theme.MOSS
                    ),
                )
            )
        self._restore_cursor(table, row, len(rows))

        ours = sum(
            1
            for listener in rows
            if (getattr(listener, "node", ""), listener.host, listener.port) in known
        )
        hidden = sum(1 for listener in rows if listener.pid is None)
        stats = Text()
        stats.append(f"{len(rows)} listening", style=theme.BONE_DIM)
        stats.append(SEP, style=theme.VEIN_BRIGHT)
        stats.append(str(ours), style=theme.MOSS)
        stats.append(" handed out by warden", style=theme.BONE_DIM)
        if hidden:
            stats.append(SEP, style=theme.VEIN_BRIGHT)
            stats.append(f"{hidden} owned by another user", style=theme.SHRIEKER)
        self._say(self._missing(stats))

    def _load_rules(self) -> None:
        if not self.fleet:
            found = self.client.firewall_rules()
            self.call_from_thread(
                self.show_rules, [rule.model_dump(mode="json") for rule in found]
            )
            return
        found = self.client.fleet_firewall_rules()
        self.call_from_thread(self.show_rules, found.rules, found.unreachable)

    def show_rules(
        self, rows: list[dict[str, object]], unreachable: list[Unreachable] = ()
    ) -> None:
        self._remember(rows, unreachable)
        rows = self._showing(rows)
        self._label()
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self._rules = rows
        for index, rule in enumerate(rows, start=1):
            action = str(rule.get("action", ""))
            table.add_row(
                *self._cells(
                    index,
                    _node_name(rule),
                    Text(str(rule.get("name", "")), style=theme.GLOW),
                    _dim(rule.get("direction", "")),
                    Text(action, style=RULE_ACTIONS.get(action, "")),
                    catalogue.describe(str(rule.get("protocol", "")), rule.get("ports") or []),
                    _dim(rule.get("source", "")),
                    _dim(rule.get("origin", "")),
                    _expires(rule.get("expires_at")),
                )
            )
        self._restore_cursor(table, row, len(rows))

        borrowed = sum(1 for rule in rows if str(rule.get("origin")) == "registry")
        leased = sum(1 for rule in rows if rule.get("expires_at"))
        stats = Text()
        stats.append(f"{len(rows)} written down", style=theme.BONE_DIM)
        if borrowed:
            stats.append(SEP, style=theme.VEIN_BRIGHT)
            stats.append(str(borrowed), style=theme.GLOW)
            stats.append(" opened for a registered service", style=theme.BONE_DIM)
        if leased:
            stats.append(SEP, style=theme.VEIN_BRIGHT)
            stats.append(f"{leased} closing on their own", style=theme.SHRIEKER)
        self._say(self._missing(stats))

    def _load_nodes(self) -> None:
        self.call_from_thread(self.show_nodes, self.client.nodes())

    def show_nodes(self, rows: list[Node]) -> None:
        self._unreachable = []
        if self.fleet:
            self._node_names = sorted(node.name for node in rows)
        self._label()
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self._known = rows
        for index, node in enumerate(rows, start=1):
            table.add_row(
                _dim(index),
                Text(node.name, style=theme.GLOW),
                _dim(node.url),
                _dim(node.version),
                _dim(f"{node.pool_start}-{node.pool_end}"),
                Text(node.status, style=NODE_STATES.get(node.status, theme.BONE_DIM)),
                _dim(theme.age(node.last_seen)),
            )
        self._restore_cursor(table, row, len(rows))

        quiet = sum(1 for node in rows if node.status != "online")
        stats = Text()
        stats.append(f"{theme.plural(len(rows), 'warden')} reporting in", style=theme.BONE_DIM)
        if quiet:
            stats.append(SEP, style=theme.VEIN_BRIGHT)
            stats.append(f"{quiet} gone quiet", style=theme.SHRIEKER)
        if not rows:
            stats = Text(
                "no other warden has reported in - a node announces itself with "
                "WARDEN_UPSTREAM",
                style=theme.BONE_DIM,
            )
        self._say(stats)

    def _say(self, message: Text | str) -> None:
        stats = self.query_one("#stats", Static)
        stats.remove_class("-error")
        stats.update(message)

    def show_error(self, message: str) -> None:
        stats = self.query_one("#stats", Static)
        stats.add_class("-error")
        stats.update(message)


def run(
    url: str | None = None,
    *,
    token: str | None = None,
    interval: float = 2.0,
    fleet: bool = False,
) -> None:
    timeout = FLEET_TIMEOUT if fleet else 3.0
    with WardenClient(url, token=token, timeout=timeout) as client:
        WardenApp(client, interval=interval, fleet=fleet).run()

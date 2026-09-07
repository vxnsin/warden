"""`warden setup` with every question on one screen instead of one at a time.

The prompts are still there and still work. Anything without a terminal - a
script piping answers in, a CI job - gets those. This is for the person sitting
in front of it, who would rather see what they are agreeing to.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import (
    Footer,
    Input,
    Label,
    Select,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection

from warden import theme
from warden.core import config, happenings, webhooks
from warden.core.config import PARTS as TABS
from warden.core.config import Settings
from warden.core.events import post_once
from warden.core.happenings import EVERY, wanted
from warden.tui import BANNER_MIN_HEIGHT, PALETTE

WEBHOOK_KEYS = ("webhook", "webhook_format", "webhook_events", "webhook_secret")

# A label of 34 columns beside a field of 44 needs this much before either has
# to be cut. Narrower than that, the label goes above the field instead.
NARROW = 84

# What the footer cannot say, because these belong to whatever has focus.
HINTS = "ctrl+arrows tabs - tab moves - space toggles - pgup/pgdn scrolls - enter opens a menu"

# The same, for a width that cannot hold it. A menu still looks like a menu;
# a key that scrolls looks like nothing at all.
TIGHT = "ctrl+arrows tabs - tab moves - space toggles - pgup/pgdn"

COLOURS = {
    **PALETTE,
    "moss": theme.MOSS,
    "shrieker": theme.SHRIEKER,
    "amethyst": theme.AMETHYST,
}

SHAPES = {
    webhooks.JSON: "json - the event itself, signed",
    webhooks.DISCORD: "discord - an embed in a channel",
    webhooks.SLACK: "slack - a message in a channel",
    webhooks.TEAMS: "teams - an adaptive card",
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

#form {
    height: 1fr;
    width: 1fr;
    max-width: 88;
}

TabPane {
    padding: 0 2 1 2;
    height: 100%;
    overflow-y: auto;
    background: $raised;
    scrollbar-background: $raised;
    scrollbar-color: $vein_bright;
    scrollbar-color-hover: $glow_dim;
}

Tabs { background: $sculk; }
Tabs > #tabs-list { min-height: 1; }
Tab { color: $dim; padding: 0 2; }
Tab.-active { color: $glow; text-style: bold; }
Underline > .underline--bar { color: $glow_dim; background: $vein; }
ContentSwitcher { border: round $vein; background: $raised; height: 1fr; }
ContentSwitcher:focus-within { border: round $glow_dim; }

.group { height: auto; }

.preview-name { padding-top: 1; width: auto; }

#embed-preview {
    height: auto;
    padding: 1 2;
    background: $sculk;
    border-left: outer $vein_bright;
}

.field {
    height: auto;
    padding-top: 1;
}

.name {
    width: 34;
    height: auto;
    padding-top: 1;
}

.hint {
    color: $dim;
    height: auto;
    padding-left: 34;
}

Input {
    width: 1fr;
    max-width: 44;
    background: $sculk;
    border: tall $vein;
}
Input:focus { border: tall $glow_dim; }

Select { width: 1fr; max-width: 44; }
Select > SelectCurrent { background: $sculk; border: tall $vein; }
Select:focus > SelectCurrent { border: tall $glow_dim; }

SelectionList {
    width: 1fr;
    max-width: 64;
    height: auto;
    background: $sculk;
    border: tall $vein;
}
SelectionList:focus { border: tall $glow_dim; }

Switch { background: $sculk; border: tall $vein; }
Switch:focus { border: tall $glow_dim; }
Switch > .switch--slider { color: $dim; background: $sculk; }
Switch.-on > .switch--slider { color: $glow; }

#hints {
    height: auto;
    padding-top: 1;
    color: $dim;
}

#status { height: auto; }
#status.-bad { color: $ember; }
#status.-good { color: $moss; }

/* Under NARROW columns the label no longer fits beside the field it names, so
   it goes above it and everything gives up the margins it was enjoying. */
Screen.-narrow #shell { padding: 0 1; }
Screen.-narrow #form { padding: 0 1 1 1; }
Screen.-narrow .field { layout: vertical; }
Screen.-narrow .name { width: 1fr; padding-top: 0; }
Screen.-narrow .hint { padding-left: 0; }
Screen.-narrow #hints { padding-top: 0; }
"""


class AnswerError(ValueError):
    """An answer that cannot be written down, and which field it was."""

    def __init__(self, message: str, field: str) -> None:
        super().__init__(message)
        self.field = field


def _tagline() -> Text:
    said = Text(theme.TAGLINE, style=theme.BONE_DIM)
    said.append("  ~  ")
    said.append_text(theme.byline())
    return said


class Setup(App[dict[str, object] | None]):
    """Every setting warden asks about, on one screen."""

    TITLE = "warden setup"
    CSS = STYLES

    BINDINGS: ClassVar[list[BindingType]] = [
        # Priority, so a focused input never swallows the way out.
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+t", "test", "Test webhook", priority=True),
        Binding("ctrl+q", "leave", "Quit", priority=True),
        Binding("escape", "leave", "Quit", show=False),
        # Priority as well: an input would otherwise keep the whole form still.
        Binding("pageup", "page_up", "Scroll up", show=False, priority=True),
        Binding("pagedown", "page_down", "Scroll down", show=False, priority=True),
        Binding("ctrl+right", "next_tab", "Next tab", show=False, priority=True),
        Binding("ctrl+left", "previous_tab", "Previous tab", show=False, priority=True),
    ]

    def __init__(self, settings: Settings | None = None, start: str = TABS[0]) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.start = start if start in TABS else TABS[0]
        # Edited a bit at a time, one event at a time, so they are held here
        # until the whole screen is saved.
        self._colours = dict(self.settings.webhook_colours)
        self._titles = dict(self.settings.webhook_titles)
        self._showing: str | None = None

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **COLOURS}

    def _field(self, label: str, widget: Widget, hint: str = "") -> ComposeResult:
        with Horizontal(classes="field"):
            yield Label(label, classes="name")
            yield widget
        if hint:
            yield Static(hint, classes="hint")

    def compose(self) -> ComposeResult:
        current = self.settings
        with Vertical(id="shell"):
            yield Static(theme.banner_text(), id="banner")
            yield Static(_tagline(), id="tagline")
            with TabbedContent(id="form", initial=f"tab-{self.start}"):
                with TabPane("Ports", id="tab-ports"):
                    yield from self._field(
                        "Ports to hand out",
                        Input(f"{current.pool_start}-{current.pool_end}", id="pool"),
                        "The range this warden may give a service, like 8000-8999.",
                    )
                    yield from self._field(
                        "Ports never to hand out",
                        Input(
                            ",".join(str(port) for port in sorted(current.reserved)),
                            placeholder="8080,9000-9010",
                            id="reserved",
                        ),
                        "Kept out of the pool, whatever asks for them.",
                    )
                    yield from self._field(
                        "Port warden itself listens on",
                        Input(str(current.port), id="port"),
                    )

                with TabPane("Reach", id="tab-reach"):
                    yield from self._field(
                        "Reachable from other machines",
                        Switch(current.host != "127.0.0.1", id="open"),
                        "Off means loopback only, which needs no token to be safe.",
                    )
                    with Vertical(id="open-extra", classes="group"):
                        yield from self._field(
                            "Token callers must send",
                            Input(current.token or "", password=True, id="token"),
                            "Without one, anyone who can reach this machine can hand out "
                            "and release ports.",
                        )

                with TabPane("Fleet", id="tab-fleet"):
                    yield from self._field(
                        "Reports to another warden",
                        Switch(bool(current.upstream), id="fleet-on"),
                    )
                    with Vertical(id="fleet-extra", classes="group"):
                        yield from self._field(
                            "Address of that warden",
                            Input(
                                current.upstream or "",
                                placeholder="http://hub:7010",
                                id="upstream",
                            ),
                        )
                        yield from self._field(
                            "Name for this machine", Input(current.node, id="node")
                        )
                        yield from self._field(
                            "Address it should use back",
                            Input(current.advertise_url, id="advertise"),
                        )
                        yield from self._field(
                            "Shared secret between wardens",
                            Input(current.cluster_token or "", password=True, id="cluster-token"),
                        )

                with TabPane("Events", id="tab-events"):
                    yield from self._field(
                        "Post events somewhere",
                        Switch(bool(current.webhook), id="webhook-on"),
                        "Anyone holding that address can post as you.",
                    )
                    with Vertical(id="webhook-extra", classes="group"):
                        yield from self._field(
                            "Address to post to",
                            Input(
                                current.webhook or "",
                                placeholder="https://discord.com/api/webhooks/...",
                                id="webhook",
                            ),
                        )
                        yield from self._field(
                            "Shape it should take",
                            Select(
                                [(label, name) for name, label in SHAPES.items()],
                                value=current.webhook_format,
                                allow_blank=False,
                                id="webhook-format",
                            ),
                        )
                        yield from self._field(
                            "Events worth posting",
                            SelectionList[str](
                                *(
                                    Selection(
                                        f"{happening.full:<22}{happening.means}",
                                        happening.full,
                                        wanted(
                                            current.webhook_events,
                                            happening.scope,
                                            happening.action,
                                        ),
                                    )
                                    for happening in EVERY
                                ),
                                id="webhook-events",
                            ),
                            "A channel told about every renewal is a channel people mute.",
                        )
                        with Vertical(id="secret-field", classes="group"):
                            yield from self._field(
                                "Secret to sign it with",
                                Input(
                                    current.webhook_secret or "",
                                    password=True,
                                    id="webhook-secret",
                                ),
                                "The body is signed with it, so the far end can tell the "
                                "post came from here.",
                            )
                        yield Static(
                            "The Embed tab decides what each of these looks like "
                            "in a chat window.",
                            classes="hint",
                        )

                with TabPane("Embed", id="tab-embed"):
                    yield from self._field(
                        "Event",
                        Select(
                            [(one.full, one.full) for one in happenings.EVERY],
                            value=happenings.NAMES[0],
                            allow_blank=False,
                            id="embed-which",
                        ),
                    )
                    yield from self._field(
                        "Colour",
                        Input(placeholder="#4c9a5b", id="embed-colour"),
                    )
                    yield from self._field(
                        "Words after the subject",
                        Input(placeholder="has gone quiet", id="embed-words"),
                    )
                    yield Static("Looks like", classes="name preview-name")
                    yield Static("", id="embed-preview")
                    yield Static(
                        "Each event is styled on its own, and the rest keep what they "
                        "came with. `json` carries neither - it sends the event, and "
                        "whatever reads it decides how that looks.",
                        classes="hint",
                    )

                with TabPane("Firewall", id="tab-firewall"):
                    yield from self._field(
                        "Let the registry open its ports",
                        Switch(current.firewall_from_registry, id="firewall-on"),
                        "Off means a registration can never open anything, which is "
                        "the safe answer unless you need it.",
                    )
                    with Vertical(id="firewall-extra", classes="group"):
                        yield from self._field(
                            "Networks it may open to",
                            Input(
                                ", ".join(sorted(current.firewall_allow_from)),
                                placeholder="10.0.0.0/8",
                                id="firewall-from",
                            ),
                            "Your decision, made once. Nothing declared is nothing "
                            "allowed, and a port outside the pool is never reachable.",
                        )
                        yield from self._field(
                            "Seconds to confirm a change",
                            Input(str(current.firewall_rollback), id="firewall-rollback"),
                            "A change nobody confirms undoes itself. 0 turns that off.",
                        )
                    yield from self._field(
                        "Let another machine change the rules",
                        Switch(current.allow_remote_firewall, id="firewall-remote"),
                        "Over the API, by a token holder. Reading is always allowed; "
                        "this is about changing.",
                    )
                    yield Static("", id="firewall-found", classes="hint")

                with TabPane("Risk", id="tab-risk"):
                    yield from self._field(
                        "Stopping processes over the API",
                        Switch(current.allow_kill, id="allow-kill"),
                        "Off by default. It is a much bigger thing to hand out than a port.",
                    )
            yield Static(HINTS, id="hints")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._fit()
        self._reveal()
        self.query_one("#status").display = False
        self._show_embed(happenings.NAMES[0])
        self._look_for_another_firewall()
        self._focus_start()

    def _focus_start(self) -> None:
        """Land on the first thing in the open tab that takes an answer.

        Focusing something in another tab would pull that tab to the front,
        which is how `settings edit embed` used to land on ports.
        """
        for widget in self._pane().query("Input, Select, Switch, SelectionList"):
            if widget.focusable:
                widget.focus()
                return

    @work(thread=True)
    def _look_for_another_firewall(self) -> None:
        """Say if something else is holding this machine's packets.

        Only ever says. Taking over is `warden firewall adopt`, which reads the
        other one's rules and shows them first - not something to slip into a
        settings screen.
        """
        from warden.firewall.adopt import managing

        found = managing()
        if not found:
            return
        self.call_from_thread(
            self.query_one("#firewall-found", Static).update,
            f"{theme.listed(found)} is holding this machine - "
            "`warden firewall adopt` reads its rules and takes over from it",
        )

    def on_resize(self) -> None:
        self._fit()

    def _fit(self) -> None:
        """Give up decoration before giving up any of the questions.

        An 80 by 24 terminal over ssh is a real place this gets run, and it has
        no rows to spend on a mascot.
        """
        narrow = self.size.width < NARROW
        self.screen.set_class(narrow, "-narrow")
        self.query_one("#hints", Static).update(TIGHT if narrow else HINTS)
        room = self.size.height >= BANNER_MIN_HEIGHT and not narrow
        self.query_one("#banner").display = room
        self.query_one("#tagline").display = room

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self._reveal()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in ("embed-colour", "embed-words"):
            self._draw_embed()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "embed-which":
            # The menu announces the value it was built with, and that message
            # can arrive after the screen has already loaded it - late enough
            # that treating it as a change would throw away what was typed.
            if str(event.value) == self._showing:
                return
            self._remember_embed()
            self._show_embed(str(event.value))
            return
        self._reveal()

    def _reveal(self) -> None:
        """Show only the questions the answers so far have earned."""
        self.query_one("#open-extra").display = self._on("open")
        self.query_one("#fleet-extra").display = self._on("fleet-on")
        posting = self._on("webhook-on")
        self.query_one("#webhook-extra").display = posting
        # Only the plain shape is signed, so only it is asked for a secret.
        self.query_one("#secret-field").display = posting and self._shape() == webhooks.JSON
        self.query_one("#firewall-extra").display = self._on("firewall-on")

    def _remember_embed(self) -> None:
        """Keep what was typed for the event that was on screen a moment ago."""
        which = self._showing
        if which is None:
            return
        for said, kept in (
            (self._text("embed-colour"), self._colours),
            (self._text("embed-words"), self._titles),
        ):
            if said:
                kept[which] = said
            else:
                kept.pop(which, None)

    def _show_embed(self, which: str) -> None:
        """Load one event's colour and words into the fields, and draw it."""
        self._showing = which
        self.query_one("#embed-colour", Input).value = self._colours.get(which, "")
        self.query_one("#embed-words", Input).value = self._titles.get(which, "")
        self._draw_embed()

    def _draw_embed(self) -> None:
        """What the message would look like, as it is being typed."""
        which = self._showing
        if which is None:
            return
        event = happenings.like(which)
        tint, words = self._text("embed-colour"), self._text("embed-words")
        colour, _ = webhooks.looks(event, {which: tint} if tint else None)
        line = webhooks.sentence(event, self.settings.node, {which: words} if words else None)
        shown = Text()
        shown.append("   ", style=f"on #{colour:06x}")
        shown.append("  ")
        shown.append(line)
        for name, value in webhooks.facts(event, self.settings.node)[:3]:
            shown.append(f"\n      {name}  ", style=theme.BONE_DIM)
            shown.append(value, style=theme.BONE)
        self.query_one("#embed-preview", Static).update(shown)

    def _on(self, field: str) -> bool:
        return self.query_one(f"#{field}", Switch).value

    def _text(self, field: str) -> str:
        return self.query_one(f"#{field}", Input).value.strip()

    def _shape(self) -> str:
        return str(self.query_one("#webhook-format", Select).value)

    def _chosen(self) -> list[str]:
        return sorted(self.query_one("#webhook-events", SelectionList).selected)

    def _say(self, message: str, *, bad: bool = False, good: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.set_classes(["-bad"] if bad else ["-good"] if good else [])
        status.update(message)
        # A row nobody needs is a row a 24-line terminal cannot spare.
        status.display = bool(message)

    def answers(self) -> dict[str, object]:
        """Everything on screen, as the settings file would have it."""
        answers = dict(config.stored())

        pool = self._text("pool")
        start, _, end = pool.partition("-")
        try:
            first, last = int(start), int(end or start)
        except ValueError:
            raise AnswerError(f"{pool!r} is not a range of ports, like 8000-8999", "pool") from None
        if first > last:
            raise AnswerError("the range starts after it ends", "pool")
        answers["pool_start"], answers["pool_end"] = first, last

        reserved = self._text("reserved")
        if reserved:
            try:
                answers["reserved"] = sorted(config.parse_ports(reserved))
            except ValueError:
                raise AnswerError(f"{reserved!r} is not a list of ports", "reserved") from None

        try:
            answers["port"] = int(self._text("port"))
        except ValueError:
            raise AnswerError("the port warden listens on has to be a number", "port") from None

        answers["host"] = "0.0.0.0" if self._on("open") else "127.0.0.1"
        if self._on("open"):
            answers["token"] = self._text("token")

        if self._on("fleet-on"):
            answers["upstream"] = self._text("upstream")
            answers["node"] = self._text("node")
            answers["advertise"] = self._text("advertise")
            answers["cluster_token"] = self._text("cluster-token")

        answers["allow_kill"] = self._on("allow-kill")
        answers.update(self._firewall_answers())
        answers.update(self._webhook_answers())
        self._remember_embed()
        answers["webhook_colours"] = dict(self._colours)
        answers["webhook_titles"] = dict(self._titles)
        return answers

    def _firewall_answers(self) -> dict[str, object]:
        try:
            answers: dict[str, object] = {
                "firewall_rollback": int(self._text("firewall-rollback") or 0)
            }
        except ValueError:
            raise AnswerError(
                "the seconds to wait for a confirmation have to be a number",
                "firewall-rollback",
            ) from None
        answers["allow_remote_firewall"] = self._on("firewall-remote")
        if not self._on("firewall-on"):
            # Saying no has to be able to undo a yes.
            return {**answers, "firewall_from_registry": False, "firewall_allow_from": ""}

        where = self._text("firewall-from")
        if not where:
            raise AnswerError("name the networks the registry may open ports to", "firewall-from")
        return {**answers, "firewall_from_registry": True, "firewall_allow_from": where}

    def _webhook_answers(self) -> dict[str, object]:
        if not self._on("webhook-on"):
            # Saying no has to be able to undo a yes, or the only way back is
            # editing the file this exists so nobody has to edit.
            return dict.fromkeys(WEBHOOK_KEYS, "")

        address = self._text("webhook")
        if urlparse(address).scheme not in {"http", "https"}:
            raise AnswerError("the address has to start with http:// or https://", "webhook")
        chosen = self._chosen()
        if not chosen:
            raise AnswerError("name at least one event, or turn posting off", "webhook-events")
        shape = self._shape()
        return {
            "webhook": address,
            "webhook_format": shape,
            "webhook_events": chosen,
            "webhook_secret": self._text("webhook-secret") if shape == webhooks.JSON else "",
        }

    def trial(self) -> Settings:
        """The settings a test post would go out with."""
        answers = self._webhook_answers()
        return self.settings.model_copy(
            update={
                "webhook": answers["webhook"],
                "webhook_format": answers["webhook_format"],
                "webhook_secret": answers["webhook_secret"] or None,
                "webhook_events": set(answers["webhook_events"]),
            }
        )

    def action_save(self) -> None:
        try:
            answers = self.answers()
        except AnswerError as problem:
            self._say(str(problem), bad=True)
            self.query_one(f"#{problem.field}").focus()
            return
        self.exit(answers)

    def action_leave(self) -> None:
        self.exit(None)

    def _tabs(self) -> TabbedContent:
        return self.query_one("#form", TabbedContent)

    def _pane(self) -> TabPane:
        """The tab somebody is actually looking at."""
        return self._tabs().get_pane(self._tabs().active)

    def action_page_up(self) -> None:
        self._pane().scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self._pane().scroll_page_down(animate=False)

    def action_next_tab(self) -> None:
        self._step(1)

    def action_previous_tab(self) -> None:
        self._step(-1)

    def _step(self, by: int) -> None:
        """Round the tabs, so the last one leads back to the first."""
        tabs = self._tabs()
        names = [pane.id for pane in tabs.query(TabPane) if pane.id]
        if not names:
            return
        where = names.index(tabs.active) if tabs.active in names else 0
        tabs.active = names[(where + by) % len(names)]

    def action_test(self) -> None:
        if not self._on("webhook-on"):
            self._say("There is nowhere to post to yet.", bad=True)
            return
        try:
            trial = self.trial()
        except AnswerError as problem:
            self._say(str(problem), bad=True)
            self.query_one(f"#{problem.field}").focus()
            return
        self._say("Posting a test event...")
        self._try(trial)

    @work
    async def _try(self, trial: Settings) -> None:
        problem = await post_once(trial)
        if problem:
            self._say(f"It did not arrive: {problem}", bad=True)
        else:
            self._say("It arrived.", good=True)


def run(settings: Settings | None = None, start: str = TABS[0]) -> dict[str, object] | None:
    """Ask, and hand back what to write down - or nothing if it was abandoned."""
    return Setup(settings, start).run()

import json

import pytest
from typer.testing import CliRunner

from warden.cli import app, shared
from warden.core import config
from warden.models import WebhookStatus

runner = CliRunner()

BEFORE = "\n\n\n\nn\n"
AFTER = "n\n"


def answering(*given: str) -> str:
    """The whole conversation: the questions before, these, and the one after."""
    return BEFORE + "y\n" + "".join(f"{answer}\n" for answer in given) + AFTER


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list:
    sent = []

    def remember(settings, event=None):
        sent.append(settings)
        return None

    monkeypatch.setattr(shared, "send_one", remember)
    return sent


def test_setup_writes_down_where_events_go(posted: list):
    result = runner.invoke(
        app, ["setup"], input=answering("https://chat.example/hook", "discord", "registered", "n")
    )
    assert result.exit_code == 0, result.stdout
    stored = config.stored()
    assert stored["webhook"] == "https://chat.example/hook"
    assert stored["webhook_format"] == "discord"
    assert stored["webhook_events"] == ["registered"]


def test_only_the_plain_shape_is_asked_for_a_secret(posted: list):
    runner.invoke(
        app, ["setup"], input=answering("https://chat.example/hook", "discord", "registered", "n")
    )
    assert "webhook_secret" not in config.stored()

    runner.invoke(
        app,
        ["setup"],
        input=answering("https://elsewhere.example/hook", "json", "registered", "hush", "n"),
    )
    assert config.stored()["webhook_secret"] == "hush"


def test_an_address_nothing_can_post_to_is_asked_again(posted: list):
    result = runner.invoke(
        app,
        ["setup"],
        input=answering(
            "chat.example/hook", "https://chat.example/hook", "discord", "registered", "n"
        ),
    )
    assert "not somewhere anything can be posted" in result.stdout
    assert config.stored()["webhook"] == "https://chat.example/hook"


def test_a_shape_nobody_speaks_is_asked_again(posted: list):
    result = runner.invoke(
        app,
        ["setup"],
        input=answering("https://chat.example/hook", "matrix", "discord", "registered", "n"),
    )
    assert "There is no 'matrix'" in result.stdout
    assert config.stored()["webhook_format"] == "discord"


def test_an_event_that_does_not_exist_is_asked_again(posted: list):
    result = runner.invoke(
        app,
        ["setup"],
        input=answering(
            "https://chat.example/hook", "discord", "exploded", "registered,released", "n"
        ),
    )
    assert "There is no exploded" in result.stdout
    assert config.stored()["webhook_events"] == ["registered", "released"]


def test_the_test_post_uses_what_was_just_answered(posted: list):
    runner.invoke(
        app,
        ["setup"],
        input=answering("https://chat.example/hook", "slack", "registered", "y"),
    )
    assert len(posted) == 1
    assert posted[0].webhook == "https://chat.example/hook"
    assert posted[0].webhook_format == "slack"


def test_a_test_post_that_does_not_arrive_still_writes_the_setting_down(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(shared, "send_one", lambda settings, event=None: "connection refused")
    result = runner.invoke(
        app,
        ["setup"],
        input=answering("https://chat.example/hook", "discord", "registered", "y"),
    )
    assert result.exit_code == 0
    assert "It did not arrive: connection refused" in result.stdout
    assert config.stored()["webhook"] == "https://chat.example/hook"


def test_saying_no_writes_nothing_about_webhooks(posted: list):
    runner.invoke(app, ["setup"], input=BEFORE + "n\n" + AFTER)
    assert "webhook" not in config.stored()
    assert posted == []


class FakeClient:
    def __init__(self, status: WebhookStatus) -> None:
        self.status = status

    def webhook(self) -> WebhookStatus:
        return self.status

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def serving(monkeypatch: pytest.MonkeyPatch, status: WebhookStatus) -> None:
    monkeypatch.setattr(shared, "_client", lambda url, token: FakeClient(status))


def test_the_command_says_where_events_go(monkeypatch: pytest.MonkeyPatch):
    serving(
        monkeypatch,
        WebhookStatus(
            configured=True,
            target="https://chat.example/...",
            format="discord",
            actions=["registered", "released"],
            delivered=7,
        ),
    )
    result = runner.invoke(app, ["webhook"])
    assert result.exit_code == 0
    assert "https://chat.example/..." in result.stdout
    assert "discord" in result.stdout
    assert "7" in result.stdout


def test_the_command_says_when_there_is_nowhere_to_post(monkeypatch: pytest.MonkeyPatch):
    serving(monkeypatch, WebhookStatus(configured=False))
    result = runner.invoke(app, ["webhook"])
    assert "posts events nowhere" in result.stdout


def test_the_last_failure_is_shown_rather_than_kept_quiet(monkeypatch: pytest.MonkeyPatch):
    serving(
        monkeypatch,
        WebhookStatus(
            configured=True,
            target="https://chat.example/...",
            format="json",
            failed=3,
            last_error="502 Bad Gateway",
        ),
    )
    result = runner.invoke(app, ["webhook"])
    assert "502 Bad Gateway" in result.stdout


def test_testing_without_anywhere_to_post_says_so(monkeypatch: pytest.MonkeyPatch):
    result = runner.invoke(app, ["webhook", "--test"])
    assert result.exit_code == 1
    assert "nothing to post to" in result.stderr


def test_testing_posts_one_event_and_says_where(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WARDEN_WEBHOOK", "https://chat.example/services/T0/B0/xxxx")
    monkeypatch.setattr(shared, "send_one", lambda settings, event=None: None)
    result = runner.invoke(app, ["webhook", "--test"])
    assert result.exit_code == 0
    assert "posted to https://chat.example/..." in result.stdout
    assert "xxxx" not in result.stdout


def test_a_test_that_fails_is_worth_a_non_zero_exit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WARDEN_WEBHOOK", "https://chat.example/hook")
    monkeypatch.setattr(shared, "send_one", lambda settings, event=None: "timed out")
    result = runner.invoke(app, ["webhook", "--test"])
    assert result.exit_code == 1
    assert "it did not arrive: timed out" in result.stderr


def test_a_test_can_be_read_by_a_machine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WARDEN_WEBHOOK", "https://chat.example/hook")
    monkeypatch.setattr(shared, "send_one", lambda settings, event=None: "timed out")
    result = runner.invoke(app, ["webhook", "--test", "--json"])
    assert json.loads(result.stdout) == {
        "target": "https://chat.example/...",
        "posted": False,
        "error": "timed out",
    }


def test_setup_says_a_running_warden_keeps_what_it_started_with(posted: list):
    """Writing a webhook down and watching nothing arrive is a puzzling hour."""
    result = runner.invoke(app, ["setup"], input=BEFORE + "n\n" + AFTER)
    assert "already running keeps the settings it started with" in result.stdout


class Terminal:
    def isatty(self) -> bool:
        return True


class Pipe:
    def isatty(self) -> bool:
        return False


def at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared.sys, "stdin", Terminal())
    monkeypatch.setattr(shared.sys, "stdout", Terminal())


def test_a_terminal_on_linux_can_draw_a_screen(monkeypatch: pytest.MonkeyPatch):
    at_a_terminal(monkeypatch)
    monkeypatch.setattr(shared.sys, "platform", "linux")
    monkeypatch.setenv("TERM", "xterm-256color")
    assert shared._has_a_screen()


def test_a_terminal_that_cannot_draw_one_is_not_asked_to(monkeypatch: pytest.MonkeyPatch):
    """A cron job on a Linux box has a terminal and no way to paint on it."""
    at_a_terminal(monkeypatch)
    monkeypatch.setattr(shared.sys, "platform", "linux")
    for term in ("dumb", ""):
        monkeypatch.setenv("TERM", term)
        assert not shared._has_a_screen()


def test_windows_says_nothing_about_term(monkeypatch: pytest.MonkeyPatch):
    at_a_terminal(monkeypatch)
    monkeypatch.setattr(shared.sys, "platform", "win32")
    monkeypatch.delenv("TERM", raising=False)
    assert shared._has_a_screen()


def test_answers_piped_in_never_open_a_screen(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shared.sys, "stdin", Pipe())
    monkeypatch.setattr(shared.sys, "stdout", Terminal())
    monkeypatch.setenv("TERM", "xterm-256color")
    assert not shared._has_a_screen()


def test_the_dashboard_says_so_rather_than_falling_over(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shared, "_has_a_screen", lambda: False)
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 1
    assert "cannot draw a screen" in result.stderr
    assert "warden ls" in result.stderr

import asyncio

import pytest
from textual.widgets import Input, Select, SelectionList, Static, Switch

from warden import wizard
from warden.config import Settings
from warden.wizard import Setup


def settings(**overrides) -> Settings:
    return Settings(update_check=False, **overrides)


def asking(scenario, current: Settings | None = None, size=(108, 46)):
    """Open the wizard, do something to it, and hand back what it exited with."""

    async def main():
        app = Setup(current or settings())
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(app, pilot)
        return app.return_value

    return asyncio.run(main())


def text_of(app: Setup, selector: str) -> str:
    return str(app.query_one(selector, Static).content)


def test_it_opens_showing_what_is_already_written_down():
    async def scenario(app: Setup, pilot) -> None:
        assert app.query_one("#pool", Input).value == "8100-8199"
        assert app.query_one("#port", Input).value == "7011"
        assert app.query_one("#webhook-format", Select).value == "slack"

    asking(
        scenario,
        settings(pool_start=8100, pool_end=8199, port=7011, webhook_format="slack"),
    )


def test_questions_nobody_has_earned_are_not_on_screen():
    async def scenario(app: Setup, pilot) -> None:
        assert not app.query_one("#open-extra").display
        assert not app.query_one("#fleet-extra").display
        assert not app.query_one("#webhook-extra").display

    asking(scenario)


def test_turning_posting_on_asks_where():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#webhook-on", Switch).value = True
        await pilot.pause()
        assert app.query_one("#webhook-extra").display

    asking(scenario)


def test_only_the_signed_shape_is_asked_for_a_secret():
    async def scenario(app: Setup, pilot) -> None:
        assert app.query_one("#secret-field").display
        app.query_one("#webhook-format", Select).value = "discord"
        await pilot.pause()
        assert not app.query_one("#secret-field").display

    asking(scenario, settings(webhook="https://chat.example/hook", webhook_format="json"))


def test_saving_hands_back_what_is_on_screen():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#pool", Input).value = "9000-9099"
        app.query_one("#reserved", Input).value = "9050"
        app.query_one("#allow-kill", Switch).value = True
        await pilot.press("ctrl+s")

    answers = asking(scenario)
    assert answers["pool_start"] == 9000
    assert answers["pool_end"] == 9099
    assert answers["reserved"] == [9050]
    assert answers["allow_kill"] is True
    assert answers["host"] == "127.0.0.1"


def test_leaving_writes_nothing():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#pool", Input).value = "9000-9099"
        await pilot.press("ctrl+q")

    assert asking(scenario) is None


def test_a_range_that_is_not_a_range_is_said_rather_than_saved():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#pool", Input).value = "eight thousand"
        await pilot.press("ctrl+s")
        assert "not a range of ports" in text_of(app, "#status")
        assert app.focused is app.query_one("#pool")
        await pilot.press("ctrl+q")

    assert asking(scenario) is None


def test_a_range_that_ends_before_it_starts_is_refused():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#pool", Input).value = "9000-8000"
        await pilot.press("ctrl+s")
        assert "starts after it ends" in text_of(app, "#status")
        await pilot.press("ctrl+q")

    assert asking(scenario) is None


def test_an_address_nothing_can_post_to_is_refused():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#webhook", Input).value = "chat.example/hook"
        await pilot.press("ctrl+s")
        assert "http:// or https://" in text_of(app, "#status")
        assert app.focused is app.query_one("#webhook")
        await pilot.press("ctrl+q")

    assert asking(scenario, settings(webhook="https://chat.example/hook")) is None


def test_posting_nothing_at_all_is_refused():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#webhook-events", SelectionList).deselect_all()
        await pilot.press("ctrl+s")
        assert "at least one event" in text_of(app, "#status")
        await pilot.press("ctrl+q")

    assert asking(scenario, settings(webhook="https://chat.example/hook")) is None


def test_saying_no_takes_a_webhook_back_off():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#webhook-on", Switch).value = False
        await pilot.pause()
        await pilot.press("ctrl+s")

    answers = asking(scenario, settings(webhook="https://chat.example/hook"))
    assert answers["webhook"] == ""
    assert answers["webhook_format"] == ""
    assert answers["webhook_events"] == ""


def test_a_secret_is_not_kept_for_a_shape_that_does_not_sign():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#webhook-format", Select).value = "teams"
        await pilot.pause()
        await pilot.press("ctrl+s")

    answers = asking(
        scenario,
        settings(
            webhook="https://chat.example/hook", webhook_format="json", webhook_secret="hush"
        ),
    )
    assert answers["webhook_format"] == "teams"
    assert answers["webhook_secret"] == ""


def test_a_test_post_with_nowhere_to_go_says_so():
    async def scenario(app: Setup, pilot) -> None:
        await pilot.press("ctrl+t")
        assert "nowhere to post to" in text_of(app, "#status")
        await pilot.press("ctrl+q")

    asking(scenario)


def test_a_test_post_that_arrives_says_it_arrived(monkeypatch: pytest.MonkeyPatch):
    tried = []

    async def arriving(settings, event=None):
        tried.append(settings)
        return None

    monkeypatch.setattr(wizard, "post_once", arriving)

    async def scenario(app: Setup, pilot) -> None:
        await pilot.press("ctrl+t")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(app, "#status") == "It arrived."
        await pilot.press("ctrl+q")

    asking(scenario, settings(webhook="https://chat.example/hook", webhook_format="slack"))
    assert tried[0].webhook == "https://chat.example/hook"
    assert tried[0].webhook_format == "slack"


def test_a_test_post_that_does_not_arrive_says_why(monkeypatch: pytest.MonkeyPatch):
    async def refused(settings, event=None):
        return "connection refused"

    monkeypatch.setattr(wizard, "post_once", refused)

    async def scenario(app: Setup, pilot) -> None:
        await pilot.press("ctrl+t")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "connection refused" in text_of(app, "#status")
        await pilot.press("ctrl+q")

    asking(scenario, settings(webhook="https://chat.example/hook"))


def test_the_test_post_uses_the_address_on_screen_not_the_one_on_disk(
    monkeypatch: pytest.MonkeyPatch,
):
    """Trying it out before saving is the whole point of trying it out."""
    tried = []

    async def arriving(settings, event=None):
        tried.append(settings.webhook)
        return None

    monkeypatch.setattr(wizard, "post_once", arriving)

    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#webhook", Input).value = "https://elsewhere.example/hook"
        await pilot.press("ctrl+t")
        await app.workers.wait_for_complete()
        await pilot.press("ctrl+q")

    asking(scenario, settings(webhook="https://chat.example/hook"))
    assert tried == ["https://elsewhere.example/hook"]


def test_the_fleet_answers_are_only_written_when_it_reports_to_one():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#fleet-on", Switch).value = True
        await pilot.pause()
        app.query_one("#upstream", Input).value = "http://hub:7010"
        app.query_one("#node", Input).value = "build-01"
        await pilot.press("ctrl+s")

    answers = asking(scenario)
    assert answers["upstream"] == "http://hub:7010"
    assert answers["node"] == "build-01"


def test_a_warden_left_on_loopback_is_not_asked_for_a_token():
    async def scenario(app: Setup, pilot) -> None:
        await pilot.press("ctrl+s")

    answers = asking(scenario)
    assert answers["host"] == "127.0.0.1"
    assert "token" not in answers


def test_the_events_already_chosen_start_ticked_and_the_others_do_not():
    async def scenario(app: Setup, pilot) -> None:
        chosen = app.query_one("#webhook-events", SelectionList)
        assert sorted(chosen.selected) == ["expired", "registered"]

    asking(
        scenario,
        settings(webhook="https://chat.example/hook", webhook_events={"registered", "expired"}),
    )


def test_a_narrow_terminal_puts_the_label_above_the_field_it_names():
    async def scenario(app: Setup, pilot) -> None:
        assert app.screen.has_class("-narrow")
        assert not app.query_one("#banner").display
        assert not app.query_one("#tagline").display

    asking(scenario, size=(80, 24))


def test_a_terminal_with_room_keeps_the_mascot():
    async def scenario(app: Setup, pilot) -> None:
        assert not app.screen.has_class("-narrow")
        assert app.query_one("#banner").display

    asking(scenario, size=(120, 44))


def test_nothing_is_cut_off_on_an_eighty_column_terminal():
    """80 by 24 over ssh is a real place this gets run."""

    async def scenario(app: Setup, pilot) -> None:
        edge = app.query_one("#form").content_region.right
        for widget in app.query(Input):
            if widget.display:
                assert widget.region.right <= edge, widget.id

    asking(scenario, settings(webhook="https://chat.example/hook"), size=(80, 24))


def test_the_controls_are_on_screen_rather_than_assumed():
    async def scenario(app: Setup, pilot) -> None:
        hints = text_of(app, "#hints")
        assert "tab" in hints and "space" in hints and "pgup/pgdn" in hints
        assert not app.query_one("#status").display

    asking(scenario, size=(80, 24))


def test_a_terminal_with_room_names_every_key_it_can():
    async def scenario(app: Setup, pilot) -> None:
        hints = text_of(app, "#hints")
        for key in ("tab", "space", "enter", "pgup/pgdn"):
            assert key in hints

    asking(scenario, size=(120, 44))


def test_a_message_takes_its_row_only_while_there_is_one():
    async def scenario(app: Setup, pilot) -> None:
        app.query_one("#pool", Input).value = "nonsense"
        await pilot.press("ctrl+s")
        assert app.query_one("#status").display
        await pilot.press("ctrl+q")

    asking(scenario, size=(80, 24))


def test_it_stays_in_one_piece_on_a_very_small_terminal():
    async def scenario(app: Setup, pilot) -> None:
        assert app.query_one("#hints").display
        assert app.query_one("#form").size.height > 0

    asking(scenario, size=(60, 16))


def test_the_form_scrolls_without_leaving_the_field_you_are_in():
    async def scenario(app: Setup, pilot) -> None:
        form = app.query_one("#form")
        assert form.scroll_offset.y == 0
        focused = app.focused
        await pilot.press("pagedown")
        await pilot.pause()
        assert form.scroll_offset.y > 0
        assert app.focused is focused
        await pilot.press("pageup")
        await pilot.pause()
        assert form.scroll_offset.y == 0
        await pilot.press("ctrl+q")

    asking(scenario, settings(webhook="https://chat.example/hook"), size=(80, 24))


def test_the_scroll_keys_are_named_where_they_can_be_read():
    async def scenario(app: Setup, pilot) -> None:
        assert "pgup/pgdn" in text_of(app, "#hints")

    asking(scenario, size=(120, 44))


def test_a_narrow_hint_keeps_the_keys_that_look_like_nothing():
    """A menu still looks like a menu. A scroll key looks like nothing at all."""

    async def scenario(app: Setup, pilot) -> None:
        hints = text_of(app, "#hints")
        assert "pgup/pgdn" in hints
        assert "space toggles" in hints
        assert len(hints) < 60

    asking(scenario, size=(60, 16))

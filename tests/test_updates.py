import asyncio
import sys

import httpx
import pytest

from warden import __version__, updates
from warden.config import Settings
from warden.errors import NotPermittedError, UpdateFailedError


def answering(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def check(transport: httpx.MockTransport, repo: str = "vxnsin/warden"):
    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            return await updates.check(http, repo)

    return asyncio.run(main())


def release(tag: str, url: str = "https://example.invalid/r/1"):
    return answering(
        lambda request: httpx.Response(200, json={"tag_name": tag, "html_url": url})
    )


def test_a_higher_version_is_worth_moving_to():
    assert updates.newer("0.2.0", "0.1.0") is True
    assert updates.newer("v1.0.0", "0.9.9") is True


def test_the_same_or_older_version_is_not():
    assert updates.newer("0.1.0", "0.1.0") is False
    assert updates.newer("0.0.9", "0.1.0") is False


def test_a_prerelease_does_not_count_as_newer_than_the_release():
    assert updates.newer("0.2.0rc1", "0.2.0") is False


def test_a_tag_that_is_not_a_version_is_ignored_rather_than_crashing():
    assert updates.newer("nightly", "0.1.0") is False


def test_a_newer_release_is_reported_with_its_link():
    status = check(release("v9.9.9"))
    assert status.available is True
    assert status.latest == "9.9.9"
    assert status.url == "https://example.invalid/r/1"
    assert status.current == __version__


def test_running_the_newest_is_not_reported_as_an_update():
    status = check(release(__version__))
    assert status.available is False
    assert status.latest == __version__


def test_a_project_with_no_releases_says_so_plainly():
    status = check(answering(lambda request: httpx.Response(404, json={})))
    assert status.available is False
    assert status.latest is None
    assert "no releases yet" in status.reason


def test_no_network_is_a_reason_not_a_crash():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    status = check(answering(refuse))
    assert status.available is False
    assert status.reason


def test_being_rate_limited_is_a_reason_not_a_crash():
    status = check(answering(lambda request: httpx.Response(403, json={})))
    assert status.available is False
    assert status.reason


def test_a_release_without_a_tag_is_not_mistaken_for_one():
    status = check(answering(lambda request: httpx.Response(200, json={"tag_name": ""})))
    assert status.latest is None
    assert "no tag" in status.reason


def test_the_repository_asked_about_is_the_configured_one():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, json={})

    check(answering(handler), repo="someone/else")
    assert seen == ["https://api.github.com/repos/someone/else/releases/latest"]


def settings(**kwargs) -> Settings:
    return Settings(update_check=False, **kwargs)


def test_updating_is_refused_unless_it_is_switched_on():
    with pytest.raises(NotPermittedError, match="WARDEN_ALLOW_REMOTE_UPDATE"):
        updates.apply(settings(update_command="echo hi"))


def test_a_warden_with_no_command_does_not_know_how_to_update():
    with pytest.raises(NotPermittedError, match="no WARDEN_UPDATE_COMMAND"):
        updates.apply(settings(allow_remote_update=True))


def test_the_configured_command_is_what_runs():
    output = updates.apply(
        settings(
            allow_remote_update=True,
            update_command=f'{sys.executable} -c "print(\'updated to the moon\')"',
        )
    )
    assert "updated to the moon" in output


def test_a_command_that_fails_reports_its_output():
    with pytest.raises(UpdateFailedError, match="exited 3"):
        updates.apply(
            settings(
                allow_remote_update=True,
                update_command=f'{sys.executable} -c "raise SystemExit(3)"',
            )
        )


def test_a_command_that_is_not_there_says_so():
    with pytest.raises(UpdateFailedError, match="cannot run"):
        updates.apply(
            settings(allow_remote_update=True, update_command="definitely-not-a-program")
        )


def test_a_command_that_hangs_is_given_up_on():
    with pytest.raises(UpdateFailedError, match="longer than"):
        updates.apply(
            settings(
                allow_remote_update=True,
                update_command=f'{sys.executable} -c "import time; time.sleep(30)"',
            ),
            timeout=1.0,
        )


def test_the_watcher_stays_quiet_when_checking_is_off():
    watcher = updates.UpdateWatcher(Settings(update_check=False))

    async def scenario() -> None:
        watcher.start()
        assert watcher._task is None
        await watcher.stop()

    asyncio.run(scenario())


def test_the_watcher_has_an_answer_before_it_has_asked():
    watcher = updates.UpdateWatcher(Settings(update_check=False))
    assert watcher.status.current == __version__
    assert watcher.status.available is False

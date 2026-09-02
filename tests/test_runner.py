import os
import sys
from datetime import UTC, datetime

import pytest

from warden import runner
from warden.models import Registration


def registration(port: int = 8000, name: str = "shop-api") -> Registration:
    now = datetime.now(UTC)
    return Registration(
        name=name,
        kind="backend",
        project=None,
        host="127.0.0.1",
        port=port,
        pid=None,
        meta={},
        ttl=None,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )


def python(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def test_the_name_defaults_to_the_directory(tmp_path, monkeypatch):
    (tmp_path / "Shop API").mkdir()
    monkeypatch.chdir(tmp_path / "Shop API")
    assert runner.default_name() == "shop-api"


def test_the_child_is_told_the_port_the_way_frameworks_read_it():
    env = runner.environment(registration())
    assert env["PORT"] == "8000"
    assert env["WARDEN_PORT"] == "8000"
    assert env["WARDEN_ADDRESS"] == "127.0.0.1:8000"
    assert env["WARDEN_SERVICE"] == "shop-api"


def test_the_child_keeps_the_environment_it_would_have_had(monkeypatch):
    monkeypatch.setenv("SOMETHING_ELSE", "kept")
    assert runner.environment(registration())["SOMETHING_ELSE"] == "kept"


def test_the_port_reaches_the_process():
    code = runner.supervise(
        python("import os, sys; sys.exit(0 if os.environ['PORT'] == '8000' else 1)"),
        registration(),
    )
    assert code == 0


def test_the_exit_code_is_handed_back():
    # A wrapper that swallows this breaks every script it is put in front of.
    assert runner.supervise(python("raise SystemExit(3)"), registration()) == 3
    assert runner.supervise(python("pass"), registration()) == 0


def test_the_pid_is_reported_once_the_child_exists():
    seen: list[int] = []
    runner.supervise(python("pass"), registration(), seen.append)
    assert len(seen) == 1
    assert seen[0] != os.getpid()


def test_a_command_that_is_not_there_is_not_silently_ignored():
    with pytest.raises(OSError):
        runner.supervise(["definitely-not-a-program"], registration())


def test_a_free_port_comes_out_of_the_pool():
    port = runner.free_port()
    assert 8000 <= port <= 8999


def test_the_free_port_is_actually_free():
    from warden.allocator import is_bound

    assert is_bound("127.0.0.1", runner.free_port()) is False


def test_a_heartbeat_renews_until_it_is_stopped():
    class Counting:
        def __init__(self) -> None:
            self.beats = 0

        def heartbeat(self, name, *, pid=None, ttl=None):
            self.beats += 1

    client = Counting()
    beat = runner.Heartbeat(client, "shop-api", ttl=3)
    assert beat.interval == 1.0
    beat.start()
    beat._stop.wait(2.2)
    beat.stop()
    assert client.beats >= 1


def test_a_heartbeat_that_cannot_reach_the_warden_does_not_take_the_process_down():
    from warden.errors import WardenError

    class Unreachable:
        def heartbeat(self, name, *, pid=None, ttl=None):
            raise WardenError("no warden reachable")

    beat = runner.Heartbeat(Unreachable(), "shop-api", ttl=3)
    beat.start()
    beat._stop.wait(1.5)
    beat.stop()
    assert beat._thread is not None and not beat._thread.is_alive()


def test_the_environment_to_print_carries_only_wardens_own():
    values = runner.as_env(registration())
    assert set(values) == {
        "PORT", "WARDEN_PORT", "WARDEN_HOST", "WARDEN_ADDRESS", "WARDEN_SERVICE"
    }


def test_a_dotenv_gains_the_keys_it_did_not_have():
    merged = runner.merge_dotenv("DEBUG=true\n", {"PORT": "8000"})
    assert merged == "DEBUG=true\nPORT=8000\n"


def test_a_key_already_there_is_replaced_where_it_stands():
    merged = runner.merge_dotenv(
        "DATABASE_URL=postgres://localhost/shop\nPORT=9999\nDEBUG=true\n",
        {"PORT": "8000"},
    )
    assert merged.splitlines() == [
        "DATABASE_URL=postgres://localhost/shop",
        "PORT=8000",
        "DEBUG=true",
    ]


def test_lines_warden_did_not_write_are_left_exactly_as_they_were():
    # A .env is usually somebody else's file too.
    before = "# ports below\n\nDATABASE_URL=postgres://x\nSECRET=  spaced  \n"
    merged = runner.merge_dotenv(before, {"PORT": "8000"})
    for line in before.strip("\n").splitlines():
        assert line in merged.splitlines()


def test_an_empty_file_just_gets_the_keys():
    assert runner.merge_dotenv("", {"PORT": "8000"}) == "PORT=8000\n"

import pytest
from typer.testing import CliRunner

from warden import config
from warden.cli import app
from warden.config import Settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def somewhere_of_our_own(tmp_path, monkeypatch):
    """A config file per test, and a working directory with no .env in it."""
    monkeypatch.setenv("WARDEN_CONFIG", str(tmp_path / "warden.toml"))
    monkeypatch.chdir(tmp_path)


def test_no_file_means_defaults():
    assert config.stored() == {}
    assert Settings().pool_start == 8000


def test_what_is_written_is_what_is_read():
    config.write({"pool_start": 4000, "pool_end": 4099})
    assert Settings().pool_start == 4000
    assert Settings().pool_end == 4099


def test_the_environment_wins_over_the_file(monkeypatch):
    config.write({"pool_start": 4000, "pool_end": 4099})
    monkeypatch.setenv("WARDEN_POOL_START", "4050")
    assert Settings().pool_start == 4050


def test_a_flag_wins_over_the_file():
    config.write({"pool_start": 4000, "pool_end": 4099})
    assert Settings(pool_start=4010).pool_start == 4010


def test_a_dotenv_beside_the_process_wins_over_the_file(tmp_path):
    config.write({"pool_start": 4000, "pool_end": 4099})
    (tmp_path / ".env").write_text("WARDEN_POOL_START=4020\n")
    assert Settings().pool_start == 4020


def test_where_a_value_came_from(monkeypatch, tmp_path):
    config.write({"pool_start": 4000, "pool_end": 4099})
    monkeypatch.setenv("WARDEN_TOKEN", "from-the-environment")
    (tmp_path / ".env").write_text("WARDEN_HOST=10.0.0.7\n")
    assert config.origin("pool_start") == "config file"
    assert config.origin("token") == "environment"
    assert config.origin("host") == ".env"
    assert config.origin("node_ttl") == "default"


def test_empty_values_are_left_out_rather_than_written_as_nothing():
    config.write({"pool_start": 4000, "token": None, "update_command": ""})
    assert config.stored() == {"pool_start": 4000}


def test_types_survive_the_round_trip():
    config.write({"probe": False, "port": 7011, "reserved": {9000, 8080}})
    stored = config.stored()
    assert stored["probe"] is False
    assert stored["port"] == 7011
    assert stored["reserved"] == [8080, 9000]


def test_a_string_with_a_quote_in_it_does_not_break_the_file():
    config.write({"update_command": 'say "hello"'})
    assert config.stored()["update_command"] == 'say "hello"'


def test_settings_lists_every_field_and_its_source():
    config.write({"pool_start": 4000})
    result = runner.invoke(app, ["settings"])
    assert result.exit_code == 0
    assert "pool_start" in result.output
    assert "config file" in result.output
    assert "default" in result.output


def test_a_secret_is_never_printed():
    config.write({"token": "do-not-show-this"})
    result = runner.invoke(app, ["settings"])
    assert "do-not-show-this" not in result.output
    assert "set" in result.output


def test_set_writes_the_value_typed():
    assert runner.invoke(app, ["settings", "set", "pool_start", "4000"]).exit_code == 0
    assert config.stored()["pool_start"] == 4000


def test_set_normalises_what_it_stores():
    runner.invoke(app, ["settings", "set", "node", "Build-01.Office"])
    assert config.stored()["node"] == "build-01.office"


def test_set_parses_a_range_of_reserved_ports():
    runner.invoke(app, ["settings", "set", "reserved", "8080,9000-9002"])
    assert config.stored()["reserved"] == [8080, 9000, 9001, 9002]


def test_reserved_records_what_was_asked_for_not_what_warden_adds():
    runner.invoke(app, ["settings", "set", "pool_start", "7000"])
    runner.invoke(app, ["settings", "set", "pool_end", "7999"])
    runner.invoke(app, ["settings", "set", "reserved", "7500"])
    # 7010 falls inside that pool and warden reserves it on its own; the file
    # should still say only what was typed.
    assert config.stored()["reserved"] == [7500]
    assert 7010 in Settings().reserved


def test_a_value_out_of_range_is_refused_and_nothing_is_written():
    result = runner.invoke(app, ["settings", "set", "port", "99999"])
    assert result.exit_code == 1
    assert config.stored() == {}


def test_a_setting_that_does_not_exist_is_refused():
    result = runner.invoke(app, ["settings", "set", "nonsense", "1"])
    assert result.exit_code == 1
    assert "lists them all" in result.output


def test_set_says_when_the_environment_will_win_anyway(monkeypatch):
    monkeypatch.setenv("WARDEN_POOL_START", "5000")
    result = runner.invoke(app, ["settings", "set", "pool_start", "4000"])
    assert result.exit_code == 0
    assert "environment still wins" in result.output


def test_unset_puts_a_setting_back_to_its_default():
    runner.invoke(app, ["settings", "set", "probe", "false"])
    assert Settings().probe is False
    assert runner.invoke(app, ["settings", "unset", "probe"]).exit_code == 0
    assert Settings().probe is True


def test_unsetting_something_that_is_not_there_says_so():
    result = runner.invoke(app, ["settings", "unset", "probe"])
    assert result.exit_code == 1
    assert "not in the config file" in result.output


def test_setup_writes_the_answers_it_was_given():
    result = runner.invoke(app, ["setup"], input="4000-4099\n8080\n7011\nn\nn\nn\n")
    assert result.exit_code == 0
    stored = config.stored()
    assert (stored["pool_start"], stored["pool_end"]) == (4000, 4099)
    assert stored["reserved"] == [8080]
    assert stored["port"] == 7011
    assert stored["host"] == "127.0.0.1"


def test_setup_pressing_enter_throughout_keeps_the_defaults():
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\n\n")
    assert result.exit_code == 0
    assert config.stored()["pool_start"] == 8000


def test_setup_keeps_what_it_was_not_asked_about():
    config.write({"update_command": "/usr/local/bin/update-warden.sh"})
    runner.invoke(app, ["setup"], input="\n\n\n\n\n\n")
    assert config.stored()["update_command"] == "/usr/local/bin/update-warden.sh"

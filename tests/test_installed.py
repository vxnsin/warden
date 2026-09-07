"""Working out how warden got onto a machine, which says how it leaves."""

from pathlib import Path, PureWindowsPath

import pytest

from warden.core import installed


def test_a_uv_tool_is_upgraded_by_name():
    found = installed._uv_tool(Path("/home/x/.local/share/uv/tools/warden-ports"))
    assert found is not None
    assert found.command == "uv tool upgrade warden-ports"
    assert found.runnable


def test_the_windows_path_to_a_uv_tool_is_read_the_same_way():
    found = installed._uv_tool(
        PureWindowsPath(r"C:\Users\x\AppData\Roaming\uv\tools\warden-ports")
    )
    assert found is not None
    assert found.command == "uv tool upgrade warden-ports"


def test_a_tool_installed_under_the_wrong_name_is_replaced_rather_than_upgraded():
    """`warden` on PyPI is somebody else's project, so upgrading it fetches theirs."""
    found = installed._uv_tool(Path("/home/x/.local/share/uv/tools/warden"))
    assert found is not None
    assert found.command == "uv tool uninstall warden && uv tool install warden-ports"
    assert found.wrong and not found.runnable
    assert "different project" in found.note


def test_pipx_is_told_apart_from_uv():
    found = installed._pipx(Path("/home/x/.local/pipx/venvs/warden-ports"))
    assert found is not None
    assert found.command == "pipx upgrade warden-ports"


def test_somewhere_that_is_neither_is_neither():
    assert installed._uv_tool(Path("/usr/lib/python3.12")) is None
    assert installed._pipx(Path("/usr/lib/python3.12")) is None


def test_a_project_venv_upgrades_the_one_package(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('name = "something-else"', encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    found = installed._project(tmp_path / ".venv")
    assert found is not None
    assert found.command == "uv sync --upgrade-package warden-ports"


def test_a_project_without_a_lock_falls_back_to_pip(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('name = "something-else"', encoding="utf-8")
    found = installed._project(tmp_path / ".venv")
    assert found is not None
    assert "pip install --upgrade warden-ports" in found.command


def test_a_directory_that_is_not_a_venv_is_not_a_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('name = "something-else"', encoding="utf-8")
    assert installed._project(tmp_path / "lib") is None


def test_a_checkout_is_advice_rather_than_a_command_to_run():
    """`git pull && uv sync` is two commands and a shell, so warden will not run it."""
    found = installed._from_source(Path("/anywhere"))
    if found is None:
        pytest.skip("not running out of a checkout")
    assert not found.runnable
    assert "git pull" in found.command


def test_pip_is_what_is_left_and_names_the_interpreter_it_belongs_to():
    found = installed._pip(Path("/usr"))
    assert found is not None
    assert found.command.endswith("-m pip install --upgrade warden-ports")


def test_something_written_down_wins_over_anything_worked_out():
    assert installed.command("say hello") == "say hello"


def test_how_always_answers():
    found = installed.how()
    assert found.how and found.where

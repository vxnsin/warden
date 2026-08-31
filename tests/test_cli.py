from typer.testing import CliRunner

from warden import __version__, theme
from warden.cli import app

runner = CliRunner()


def test_the_bare_command_introduces_itself():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert theme.TAGLINE in result.output
    assert "Usage" in result.output


def test_the_bare_command_still_lists_what_it_can_do():
    result = runner.invoke(app, [])
    for command in ("serve", "tui", "ls", "register", "release"):
        assert command in result.output


def test_the_version_is_printed_on_its_own():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__
    assert theme.TAGLINE not in result.output

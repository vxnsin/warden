"""The command line."""

from __future__ import annotations

from warden.cli import commands
from warden.cli.shared import app, console, errors

__all__ = ["app", "console", "errors", "main"]


def main() -> None:
    commands.load()
    app()


commands.load()

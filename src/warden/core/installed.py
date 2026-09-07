"""How this warden got here, and what would put a newer one in its place.

`warden update` used to say a release existed and stop there, which leaves
somebody guessing between `uv tool upgrade`, `pipx upgrade`, `pip install -U`
and a `git pull` - and guessing wrong installs a second copy beside the one
they are running. Every answer here is worked out from where this process
actually lives.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path

import warden

# What PyPI calls it. `warden` there is an unrelated project.
DISTRIBUTION = "warden-ports"

IMAGE = "ghcr.io/vxnsin/warden:latest"


@dataclass(frozen=True)
class Install:
    """Where this warden lives, and the one command that would replace it."""

    how: str
    where: str
    command: str | None = None
    note: str = ""
    # Advice rather than something to hand to a subprocess: two commands joined
    # by `&&`, or a pull that has to happen outside the container it is about.
    runnable: bool = True
    # Installed under a name that is not this project's. Worth saying whether
    # or not there is a newer version, because upgrading that name is a
    # different program.
    wrong: bool = False

    @property
    def said(self) -> str:
        return f"{self.how} - {self.where}"


def how() -> Install:
    """Work out how this warden was installed. Never raises; guesses last."""
    here = Path(sys.prefix)
    for look in (_docker, _uv_tool, _pipx, _from_source, _project, _pip):
        found = look(here)
        if found is not None:
            return found
    return Install("unknown", str(here))


def command(configured: str | None = None) -> str | None:
    """What to run to update this machine.

    A configured `update_command` always wins: somebody who wrote one down
    knows something about this machine that no amount of looking would find.
    """
    return configured or how().command


def _docker(_: Path) -> Install | None:
    if not Path("/.dockerenv").exists():
        return None
    return Install(
        "a container",
        IMAGE,
        f"docker pull {IMAGE}",
        "the container has to be recreated from the new image afterwards",
        runnable=False,
    )


def _uv_tool(prefix: Path) -> Install | None:
    """`uv tool install` puts each tool in its own directory under uv/tools."""
    name = _named_under(prefix, "uv", "tools")
    if name is None:
        return None
    if name != DISTRIBUTION:
        return _wrong_name("uv tool", prefix, name)
    return Install("uv tool", str(prefix), f"uv tool upgrade {DISTRIBUTION}")


def _pipx(prefix: Path) -> Install | None:
    name = _named_under(prefix, "pipx", "venvs")
    if name is None:
        return None
    if name != DISTRIBUTION:
        return _wrong_name("pipx", prefix, name)
    return Install("pipx", str(prefix), f"pipx upgrade {DISTRIBUTION}")


def _wrong_name(how: str, prefix: Path, name: str) -> Install:
    """Installed under a name that belongs to somebody else on PyPI.

    Upgrading that name would not fetch this program, and might fetch theirs.
    The way out is to take it off and put it back under the name it publishes
    under, which also clears a stale copy shadowing the new one on PATH.
    """
    return Install(
        how,
        str(prefix),
        f"{how} uninstall {name} && {how} install {DISTRIBUTION}",
        f"installed as `{name}`, which is a different project on PyPI; "
        f"this one publishes as `{DISTRIBUTION}`",
        runnable=False,
        wrong=True,
    )


def _named_under(prefix: Path, *trail: str) -> str | None:
    """The directory named just after ``trail``, if ``trail`` is in the path."""
    parts = [part.lower() for part in prefix.parts]
    wanted = list(trail)
    for index in range(len(parts) - len(trail)):
        if parts[index : index + len(trail)] == wanted:
            return prefix.parts[index + len(trail)]
    return None


def _from_source(_: Path) -> Install | None:
    """A checkout, whether installed with -e or just run out of its own tree."""
    root = _checkout()
    if root is None:
        return None
    return Install(
        "a checkout",
        str(root),
        "git pull && uv sync",
        "an editable install follows the checkout, so nothing else is needed",
        runnable=False,
    )


def _checkout() -> Path | None:
    """The warden repository this code is being read out of, if it is one."""
    root = Path(warden.__file__).resolve().parents[2]
    if _editable() and (root / "pyproject.toml").is_file():
        return root
    if (root / ".git").exists() and _its_own(root / "pyproject.toml"):
        return root
    return None


def _editable() -> bool:
    """Whether the installed distribution points back at a directory."""
    try:
        said = Distribution.from_name(DISTRIBUTION).read_text("direct_url.json")
    except (PackageNotFoundError, OSError):
        return False
    if not said:
        return False
    try:
        return bool(json.loads(said).get("dir_info", {}).get("editable"))
    except ValueError:
        return False


def _its_own(pyproject: Path) -> bool:
    """Whether that pyproject.toml is warden's own rather than something using it."""
    try:
        return f'name = "{DISTRIBUTION}"' in pyproject.read_text(encoding="utf-8")
    except OSError:
        return False


def _project(prefix: Path) -> Install | None:
    """A virtualenv belonging to a project that depends on warden."""
    root = prefix.parent
    if prefix.name not in (".venv", "venv") or not (root / "pyproject.toml").is_file():
        return None
    if (root / "uv.lock").is_file():
        return Install("a project", str(root), f"uv sync --upgrade-package {DISTRIBUTION}")
    return Install("a project", str(root), f"{_python()} -m pip install --upgrade {DISTRIBUTION}")


def _pip(prefix: Path) -> Install | None:
    return Install("pip", str(prefix), f"{_python()} -m pip install --upgrade {DISTRIBUTION}")


def _python() -> str:
    """The interpreter running this, quoted if its path has a space in it."""
    said = sys.executable or "python"
    return f'"{said}"' if " " in said else said

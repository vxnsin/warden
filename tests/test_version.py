import tomllib
from pathlib import Path

import pytest

from warden import __version__

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.mark.skipif(not PYPROJECT.is_file(), reason="not running from a checkout")
def test_the_two_places_the_version_lives_agree():
    """One of them reaches PyPI and the other answers `warden --version`.

    They are set by hand in different files, and a release that disagrees with
    itself is the kind of thing nobody notices until someone reports a bug
    against a version that was never built.
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert declared == __version__

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from warden.core.config import Settings
from warden.core.store import Store
from warden.errors import NotPermittedError
from warden.ports.allocator import PortPool
from warden.ports.listeners import listeners
from warden.ports.service import Registry


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    for name in list(os.environ):
        if name.startswith("WARDEN_"):
            monkeypatch.delenv(name, raising=False)
    # Never the config file of whoever is running the tests.
    monkeypatch.setenv(
        "WARDEN_CONFIG", str(tmp_path_factory.mktemp("config") / "warden.toml")
    )


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as store:
        yield store


@pytest.fixture
def manager(store: Store) -> Registry:
    return Registry(store, PortPool(8000, 8004), probe=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "registry.db",
        node="hub",
        pool_start=8000,
        pool_end=8004,
        probe=False,
        update_check=False,
    )


def _sockets_are_listable() -> bool:
    """Whether this machine will name the sockets that are open on it.

    macOS refuses to enumerate another process's sockets without root, and says
    so through warden's own error. The tests that need a real listing skip
    there rather than reporting the machine as broken.
    """
    try:
        listeners()
    except NotPermittedError:
        return False
    return True


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "sockets: needs this machine to list the sockets open on it"
    )
    # Textual reads this once, when it is first imported - at collection, which
    # is after this runs. A pressed key waits, with no timeout, for every
    # animation to finish, and switching tabs starts one twenty milliseconds
    # after the fact. Twice on Windows a ctrl+q pressed right after a refusal
    # that switched tabs never came back. Nothing here tests motion.
    os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _sockets_are_listable():
        return
    refused = pytest.mark.skip(reason="this system will not list sockets without root")
    for item in items:
        if "sockets" in item.keywords:
            item.add_marker(refused)

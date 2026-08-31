from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from port_manager.allocator import PortPool
from port_manager.config import Settings
from port_manager.service import PortManager
from port_manager.store import Store


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("PORT_MANAGER_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as store:
        yield store


@pytest.fixture
def manager(store: Store) -> PortManager:
    return PortManager(store, PortPool(8000, 8004), probe=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "registry.db",
        pool_start=8000,
        pool_end=8004,
        probe=False,
    )

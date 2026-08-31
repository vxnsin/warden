from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from warden.allocator import PortPool
from warden.config import Settings
from warden.service import Registry
from warden.store import Store


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("WARDEN_"):
            monkeypatch.delenv(name, raising=False)


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
    )

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from platformdirs import user_data_path
from pydantic import BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_URL = "http://127.0.0.1:7010"


def _parse_ports(value: object) -> object:
    """Accept ``8080``, ``"8080,9000"`` and ``"8080, 9000-9010"`` for port sets."""
    if not isinstance(value, str):
        return value
    ports: set[int] = set()
    for chunk in value.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start, _, end = chunk.partition("-")
        if end:
            ports.update(range(int(start), int(end) + 1))
        else:
            ports.add(int(start))
    return ports


PortSet = Annotated[set[int], BeforeValidator(_parse_ports)]


def default_database() -> Path:
    return user_data_path("port-manager", appauthor=False) / "registry.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PORT_MANAGER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = Field(default=7010, ge=1, le=65535)
    database: Path = Field(default_factory=default_database)

    pool_start: int = Field(default=8000, ge=1, le=65535)
    pool_end: int = Field(default=8999, ge=1, le=65535)
    reserved: PortSet = Field(default_factory=set)

    probe: bool = True
    token: str | None = None

    @model_validator(mode="after")
    def _check_pool(self) -> Settings:
        if self.pool_start > self.pool_end:
            raise ValueError("pool_start must not be greater than pool_end")
        # The API port lives on the same machine, so it can never be handed out.
        if self.pool_start <= self.port <= self.pool_end:
            self.reserved = self.reserved | {self.port}
        return self

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

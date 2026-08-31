from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from platformdirs import user_data_path
from pydantic import BeforeValidator, Field, field_validator, model_validator
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
    return user_data_path("warden", appauthor=False) / "registry.db"


def slugify(value: str) -> str:
    """Bend a machine name into something the Name pattern accepts.

    Real machine names carry capitals, spaces and dots; refusing to start over
    that would be a poor first impression for a default nobody chose.
    """
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")
    return cleaned[:64] or "warden"


def default_node() -> str:
    return slugify(socket.gethostname())


def reachable_from_elsewhere(url: str) -> bool:
    """Whether another machine could actually open this address."""
    host = urlparse(url).hostname or ""
    if host in {"", "localhost"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a name we cannot resolve here may still resolve there
    return not (address.is_loopback or address.is_unspecified)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WARDEN_",
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
    allow_kill: bool = False
    token: str | None = None

    update_check: bool = True
    update_repo: str = "vxnsin/warden"
    update_interval: int = Field(default=6 * 60 * 60, ge=300)
    allow_remote_update: bool = False
    update_command: str | None = None

    node: str = Field(default_factory=default_node)
    advertise: str | None = None
    upstream: str | None = None
    cluster_token: str | None = None
    node_ttl: int = Field(default=90, ge=10, le=86_400)

    @field_validator("node")
    @classmethod
    def _tidy_node(cls, value: str) -> str:
        return slugify(value)

    @field_validator("upstream", "advertise")
    @classmethod
    def _tidy_url(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    @model_validator(mode="after")
    def _check_pool(self) -> Settings:
        if self.pool_start > self.pool_end:
            raise ValueError("pool_start must not be greater than pool_end")
        # The API port lives on the same machine, so it can never be handed out.
        if self.pool_start <= self.port <= self.pool_end:
            self.reserved = self.reserved | {self.port}
        return self

    @model_validator(mode="after")
    def _check_advertise(self) -> Settings:
        """Refuse an address the hub could never open.

        Only when the hub is somewhere else, though: a hub on this same machine
        reaches a loopback address perfectly well, and that is how anyone tries
        the thing out before spreading it over two servers.
        """
        if not self.upstream:
            return self
        if reachable_from_elsewhere(self.upstream) and not reachable_from_elsewhere(
            self.advertise_url
        ):
            raise ValueError(
                f"the warden at {self.upstream} cannot reach this one at "
                f"{self.advertise_url}; set WARDEN_ADVERTISE to an address it can use"
            )
        return self

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def advertise_url(self) -> str:
        return self.advertise or self.url

    @property
    def role(self) -> str:
        return "edge" if self.upstream else "hub"

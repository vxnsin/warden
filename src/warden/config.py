from __future__ import annotations

import ipaddress
import os
import re
import socket
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from platformdirs import user_config_path, user_data_path
from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from warden.store import ACTIONS, NOTABLE

DEFAULT_URL = "http://127.0.0.1:7010"


def parse_ports(value: object) -> object:
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


PortSet = Annotated[set[int], BeforeValidator(parse_ports)]


def parse_words(value: object) -> object:
    """Accept ``"registered,released"`` for sets of names."""
    if not isinstance(value, str):
        return value
    return {word.strip() for word in value.replace(";", ",").split(",") if word.strip()}


WordSet = Annotated[set[str], BeforeValidator(parse_words)]


def default_database() -> Path:
    return user_data_path("warden", appauthor=False) / "registry.db"


def config_file() -> Path:
    """Where `warden setup` writes, and every warden on this machine reads."""
    override = os.environ.get("WARDEN_CONFIG")
    if override:
        return Path(override)
    return user_config_path("warden", appauthor=False) / "warden.toml"


def stored() -> dict[str, object]:
    """What the config file holds, or nothing if there is none."""
    path = config_file()
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def write(values: Mapping[str, object]) -> Path:
    """Replace the config file with these settings, dropping the empty ones."""
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {key: value for key, value in sorted(values.items()) if value not in (None, "")}
    lines = ["# Written by `warden setup`. `warden settings` edits it.", ""]
    lines += [f"{key} = {_toml(value)}" for key, value in kept.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, set | list | tuple):
        return "[" + ", ".join(_toml(item) for item in sorted(value)) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def origin(field: str) -> str:
    """Which source a setting's value came from, in the order they are consulted."""
    if f"WARDEN_{field.upper()}" in os.environ:
        return "environment"
    if Path(".env").is_file() and field.upper() in {
        key.upper().removeprefix("WARDEN_") for key in dotenv_values(".env")
    }:
        return ".env"
    if field in stored():
        return "config file"
    return "default"


def slugify(value: str) -> str:
    """Bend a machine name into something the Name pattern accepts.

    Real machine names carry capitals, spaces and dots; refusing to start over
    that would be a poor first impression for a default nobody chose.
    """
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")
    return cleaned[:64] or "warden"


def default_node() -> str:
    return slugify(socket.gethostname())


def insecure(url: str) -> bool:
    """Whether a token sent to this address would cross the network in the clear."""
    return urlparse(url).scheme != "https"


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

    webhook: str | None = None
    webhook_format: Literal["json", "discord", "slack", "teams"] = "json"
    webhook_events: WordSet = Field(default_factory=lambda: set(NOTABLE))
    webhook_secret: str | None = None

    node: str = Field(default_factory=default_node)
    advertise: str | None = None
    upstream: str | None = None
    cluster_token: str | None = None
    node_ttl: int = Field(default=90, ge=10, le=86_400)
    require_https: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # A flag beats the environment, which beats a .env beside the process,
        # which beats the file `warden setup` writes.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_file()),
            file_secret_settings,
        )

    @field_validator("node")
    @classmethod
    def _tidy_node(cls, value: str) -> str:
        return slugify(value)

    @field_validator("webhook")
    @classmethod
    def _postable(cls, value: str | None) -> str | None:
        if value and urlparse(value).scheme not in {"http", "https"}:
            raise ValueError(f"{value} is not somewhere anything can be posted")
        return value

    @field_validator("webhook_events")
    @classmethod
    def _known_events(cls, value: set[str]) -> set[str]:
        unknown = sorted(value - set(ACTIONS))
        if unknown:
            raise ValueError(
                f"no such event: {', '.join(unknown)}; there is only {', '.join(ACTIONS)}"
            )
        return value

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

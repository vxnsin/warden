from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Name = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$", strip_whitespace=True),
]
Kind = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$", strip_whitespace=True),
]
Project = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$", strip_whitespace=True),
]
Port = Annotated[int, Field(ge=1, le=65535)]


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    kind: Kind
    project: Project | None = None
    host: str = "127.0.0.1"
    preferred_port: Port | None = None
    require_port: Port | None = None
    pid: int | None = Field(default=None, ge=1)
    ttl: int | None = Field(default=None, ge=1, le=86_400)
    meta: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_wish_at_a_time(self) -> RegistrationRequest:
        if self.preferred_port is not None and self.require_port is not None:
            raise ValueError("set either preferred_port or require_port, not both")
        return self


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int | None = Field(default=None, ge=1)
    ttl: int | None = Field(default=None, ge=1, le=86_400)


class Registration(BaseModel):
    name: str
    kind: str
    project: str | None
    host: str
    port: int
    pid: int | None
    meta: dict[str, str]
    ttl: int | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class Listener(BaseModel):
    """A socket bound on this machine, whether warden handed it out or not."""

    protocol: str
    host: str
    port: int
    pid: int | None
    process: str | None
    user: str | None
    started_at: datetime | None
    command: str | None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class PoolStatus(BaseModel):
    start: int
    end: int
    size: int
    reserved: list[int]
    allocated: int
    available: int


class ErrorResponse(BaseModel):
    detail: str

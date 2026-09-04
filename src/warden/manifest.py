"""A file a project keeps beside its code, saying which ports it needs.

The point is that the answer to "which services does this project have" stops
living in whichever start script somebody wrote and becomes something that is
committed, reviewed and read by anybody who checks the repository out.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from warden.errors import WardenError

FILENAME = "warden.toml"

PROJECT_KEYS = frozenset({"name", "host"})
SERVICE_KEYS = frozenset(
    {"name", "kind", "host", "preferred_port", "require_port", "ttl", "meta"}
)


class ManifestError(WardenError):
    status_code = 400


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    kind: str
    host: str
    preferred_port: int | None = None
    require_port: int | None = None
    ttl: int | None = None
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def insists(self) -> bool:
        return self.require_port is not None


@dataclass(frozen=True)
class Manifest:
    project: str | None
    services: list[Service]
    path: Path

    @property
    def in_order(self) -> list[Service]:
        """The ones that can fail outright first.

        A service that insists on a particular port is the one that will refuse
        the whole run, so it is better to find that out before four other
        services have been registered.
        """
        return sorted(self.services, key=lambda service: not service.insists)


def _strange(keys: set[str], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(keys - allowed)
    if unknown:
        raise ManifestError(
            f"{where} has no setting called {', '.join(unknown)}; "
            f"there is only {', '.join(sorted(allowed))}"
        )


def _service(key: str, block: object, project: str | None, host: str) -> Service:
    if not isinstance(block, dict):
        raise ManifestError(f"[services.{key}] should be a table of settings")
    _strange(set(block), SERVICE_KEYS, f"[services.{key}]")
    kind = block.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ManifestError(f"[services.{key}] needs a kind, saying what the service is")
    if block.get("preferred_port") and block.get("require_port"):
        raise ManifestError(
            f"[services.{key}] asks for a preferred_port and a require_port; pick one"
        )
    return Service(
        key=key,
        name=str(block.get("name") or (f"{project}-{key}" if project else key)),
        kind=kind,
        host=str(block.get("host") or host),
        preferred_port=block.get("preferred_port"),
        require_port=block.get("require_port"),
        ttl=block.get("ttl"),
        meta={str(name): str(value) for name, value in (block.get("meta") or {}).items()},
    )


def load(path: Path) -> Manifest:
    if not path.is_file():
        raise ManifestError(f"no {path.name} here - write one, or say --file")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path.name} is not readable TOML: {exc}") from exc

    _strange(set(document), frozenset({"project", "services"}), path.name)
    project_block = document.get("project") or {}
    if not isinstance(project_block, dict):
        raise ManifestError("[project] should be a table of settings")
    _strange(set(project_block), PROJECT_KEYS, "[project]")

    services = document.get("services") or {}
    if not isinstance(services, dict) or not services:
        raise ManifestError(f"{path.name} lists no services, so there is nothing to do")

    project = project_block.get("name")
    host = str(project_block.get("host") or "127.0.0.1")
    return Manifest(
        project=str(project) if project else None,
        services=[_service(key, block, project, host) for key, block in services.items()],
        path=path,
    )


def variable(project: str | None, key: str, suffix: str) -> str:
    """`SHOP_API_PORT`, out of a project and a service key."""
    parts = [part for part in (project, key, suffix) if part]
    return re.sub(r"[^A-Z0-9]+", "_", "_".join(parts).upper()).strip("_")


def env_file(manifest: Manifest, ports: dict[str, tuple[str, int]]) -> str:
    """The whole file, every time.

    Regenerated rather than edited, and it says so at the top, because the one
    thing certain to happen otherwise is somebody editing it by hand.
    """
    lines = [
        f"# Written by `warden apply` from {manifest.path.name}. "
        "Regenerate it; do not edit it.",
    ]
    for service in manifest.services:
        host, port = ports[service.key]
        lines.append(f"{variable(manifest.project, service.key, 'host')}={host}")
        lines.append(f"{variable(manifest.project, service.key, 'port')}={port}")
    return "\n".join(lines) + "\n"

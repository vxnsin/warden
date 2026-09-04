"""A project asking for everything it needs at once."""

from __future__ import annotations

from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from warden import theme
from warden.cli import shared
from warden.cli.shared import (
    JsonOption,
    TokenOption,
    UrlOption,
    _dump,
    _fail,
    app,
    console,
    errors,
)
from warden.client import WardenClient
from warden.errors import WardenError
from warden.models import (
    Registration,
)
from warden.ports import export, manifest

ORDER = 30


APPLY_COLOURS = {
    "taken": theme.MOSS,
    "moved": theme.SHRIEKER,
    "renewed": theme.BONE_DIM,
    "released": theme.BONE_DIM,
    "gone": theme.BONE_DIM,
}

Applied = list[tuple[manifest.Service, Registration | None, str]]


def _apply_manifest(client: WardenClient, wanted: manifest.Manifest) -> Applied:
    before: dict[str, Registration] = {}
    for service in wanted.services:
        with suppress(WardenError):
            before[service.name] = client.lookup(service.name)

    held: dict[str, Registration] = {}
    taken: list[str] = []
    try:
        for service in wanted.in_order:
            held[service.key] = client.register(
                service.name,
                kind=service.kind,
                project=wanted.project,
                host=service.host,
                preferred_port=service.preferred_port,
                require_port=service.require_port,
                ttl=service.ttl,
                meta=service.meta,
            )
            if service.name not in before:
                taken.append(service.name)
    except WardenError:
        # A half-registered project is not a state anyone should have to reason
        # about, so what this run took, this run gives back.
        for name in taken:
            with suppress(WardenError):
                client.release(name)
        raise

    done: Applied = []
    for service in wanted.services:
        was = before.get(service.name)
        now = held[service.key]
        if was is None:
            what = "taken"
        elif was.port != now.port:
            what = "moved"
        else:
            what = "renewed"
        done.append((service, now, what))
    return done


def _release_manifest(client: WardenClient, wanted: manifest.Manifest) -> Applied:
    done: Applied = []
    for service in wanted.services:
        try:
            was = client.lookup(service.name)
        except WardenError:
            done.append((service, None, "gone"))
            continue
        client.release(service.name)
        done.append((service, was, "released"))
    return done


def _applied_table(done: Applied) -> Table:
    table = Table(box=None, pad_edge=False, header_style=f"bold {theme.BONE_DIM}")
    for column in ("SERVICE", "KIND", "ADDRESS", "WHAT"):
        table.add_column(column)
    for service, held, what in done:
        table.add_row(
            service.name,
            Text(service.kind, style=theme.kind_colour(service.kind)),
            held.address if held else "-",
            Text(what, style=APPLY_COLOURS.get(what, theme.BONE)),
        )
    return table


@app.command()
def apply(
    file: Annotated[
        Path, typer.Option("--file", "-f", help="Which manifest to read.")
    ] = Path(manifest.FILENAME),
    env: Annotated[
        Path | None, typer.Option(help="Also write the ports into this env file.")
    ] = None,
    release: Annotated[
        bool, typer.Option("--release", help="Give the project's ports back instead.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
    as_json: JsonOption = False,
) -> None:
    """Register everything a project's warden.toml asks for.

    Running it twice changes nothing the second time. It renews what is there
    and never shuffles a running project onto different ports.
    """
    try:
        wanted = manifest.load(file)
    except WardenError as exc:
        raise _fail(exc) from exc

    with shared._client(url, token) as client:
        try:
            done = _release_manifest(client, wanted) if release else _apply_manifest(client, wanted)
        except WardenError as exc:
            raise _fail(exc) from exc

    written = None
    if env and not release:
        ports = {service.key: (held.host, held.port) for service, held, _ in done if held}
        env.write_text(manifest.env_file(wanted, ports), encoding="utf-8")
        written = str(env)

    if as_json:
        _dump(
            {
                "project": wanted.project,
                "env": written,
                "services": [
                    {
                        "service": service.key,
                        "name": service.name,
                        "address": held.address if held else None,
                        "what": what,
                    }
                    for service, held, what in done
                ],
            }
        )
        return
    console.print(_applied_table(done))
    if written:
        console.print(f"wrote {written}", style=theme.BONE_DIM)


class Proxy(StrEnum):
    caddy = "caddy"
    nginx = "nginx"
    traefik = "traefik"


@app.command("export")
def export_config(
    proxy: Annotated[Proxy, typer.Argument(help="Which proxy this is for.")],
    project: Annotated[str | None, typer.Option(help="Only this project.")] = None,
    kind: Annotated[str | None, typer.Option(help="Only this kind of service.")] = None,
    domain: Annotated[
        str | None, typer.Option(help="Names become <service>.<domain>.")
    ] = None,
    every: Annotated[
        bool, typer.Option("--all", help="Every warden in the fleet, not just this one.")
    ] = False,
    url: UrlOption = None,
    token: TokenOption = None,
) -> None:
    """Write the proxy configuration for what is registered.

    It prints and stops. Nothing is written in place, nothing is reloaded, and
    where the result belongs stays your decision.
    """
    with shared._client(url, token) as client:
        try:
            here = client.health().node
            nodes = client.nodes() if every else []
            fleet = client.fleet_services(project=project, kind=kind) if every else None
            services = (
                fleet.services if fleet else client.services(project=project, kind=kind)
            )
        except WardenError as exc:
            raise _fail(exc) from exc

    # To stderr, so a machine that could not be asked is impossible to miss and
    # still cannot end up in the file this was redirected into.
    for missing in fleet.unreachable if fleet else []:
        errors.print(f"{missing.node} ({missing.url}) {missing.reason}", style=theme.SHRIEKER)

    print(export.render(proxy.value, services, node=here, nodes=nodes, domain=domain), end="")

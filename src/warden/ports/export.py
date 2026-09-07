"""The proxy configuration warden already knows enough to write.

Nothing here touches a file or reloads anything. It writes to stdout and stops,
because where the configuration belongs and when the proxy should pick it up
are decisions this program has no business making.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from warden.errors import WardenError
from warden.models import FleetRegistration, Node, Registration

CADDY = "caddy"
NGINX = "nginx"
TRAEFIK = "traefik"
HOSTS = "hosts"

FORMATS = (CADDY, NGINX, TRAEFIK, HOSTS)

# What warden's own lines in somebody else's file are wrapped in, so a rewrite
# can find them again and leave everything around them alone.
BEGIN = "# warden: begin"
END = "# warden: end"

# Stood in for while a builder runs, because a builder is handed the services
# and not the name of the warden they came from.
NODE_HERE = "{this warden}"


def hosts_file() -> Path:
    """Where this machine keeps the names it answers for itself."""
    if os.name == "nt":
        return Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/drivers/etc/hosts"
    return Path("/etc/hosts")

# Deliberately without a timestamp. This output belongs in a repository, and a
# header that changes every run turns every regeneration into a diff.
HEADER = "Written by `warden export` from the warden on {node}. Regenerate it; do not edit it."


# A name a resolver would accept, and nothing a proxy would read as syntax.
# `a.example.com { reverse_proxy 10.0.0.5:22 }` is one line of valid Caddy, so
# refusing control characters is not enough here - the whole shape has to be a
# hostname.
HOSTNAME = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


def hostname(service: Registration, domain: str | None) -> str:
    """What the world outside should call this service.

    A service that carries its own `domain` in its metadata means it, whatever
    anyone passed on the command line - and metadata arrives over the API, so
    it is checked here rather than trusted.
    """
    own = service.meta.get("domain")
    named = own or (f"{service.name}.{domain}" if domain else service.name)
    if not HOSTNAME.match(named):
        where = "its metadata" if own else "--domain"
        raise WardenError(
            f"{service.name} would be written as {named!r}, which is not a hostname - "
            f"check {where}"
        )
    return named

def address(service: Registration, nodes: dict[str, str]) -> str:
    """Where the proxy has to send the request.

    A service on another machine is registered under the loopback address of
    that machine, which is no use from here, so the node's own address wins.
    """
    node = getattr(service, "node", None)
    host = nodes.get(node) if node else None
    return f"{host or service.host}:{service.port}"


def node_hosts(nodes: list[Node]) -> dict[str, str]:
    return {node.name: urlsplit(node.url).hostname or node.name for node in nodes}


def _caddy(services: list[Registration], nodes: dict[str, str], domain: str | None) -> list[str]:
    lines: list[str] = []
    for service in services:
        lines += [
            f"{hostname(service, domain)} {{",
            f"\treverse_proxy {address(service, nodes)}",
            "}",
            "",
        ]
    return lines


def _nginx(services: list[Registration], nodes: dict[str, str], domain: str | None) -> list[str]:
    lines: list[str] = []
    for service in services:
        lines += [
            "server {",
            "    listen 80;",
            f"    server_name {hostname(service, domain)};",
            "",
            "    location / {",
            f"        proxy_pass http://{address(service, nodes)};",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto $scheme;",
            "    }",
            "}",
            "",
        ]
    return lines


def _traefik(
    services: list[Registration], nodes: dict[str, str], domain: str | None
) -> list[str]:
    if not services:
        return []
    routers = ["http:", "  routers:"]
    backends = ["  services:"]
    for service in services:
        routers += [
            f"    {service.name}:",
            f"      rule: Host(`{hostname(service, domain)}`)",
            f"      service: {service.name}",
        ]
        backends += [
            f"    {service.name}:",
            "      loadBalancer:",
            "        servers:",
            f"          - url: http://{address(service, nodes)}",
        ]
    return routers + backends + [""]


def _hosts(services: list[Registration], nodes: dict[str, str], domain: str | None) -> list[str]:
    """Names a resolver will answer for, which is half of what a name is for.

    A hosts file has no ports in it, so this gets somebody to the machine and
    the proxy shapes above get them to the service. Worth having anyway: most
    machines somebody is developing on have no proxy in front of anything.
    """
    lines: list[str] = [BEGIN, f"# {HEADER.format(node=NODE_HERE)}"]
    for service in services:
        where = address(service, nodes).rpartition(":")[0]
        lines.append(f"{where}\t{hostname(service, domain)}")
    lines.append(END)
    return lines


BUILDERS = {CADDY: _caddy, NGINX: _nginx, TRAEFIK: _traefik, HOSTS: _hosts}

COMMENT = {CADDY: "#", NGINX: "#", TRAEFIK: "#", HOSTS: "#"}


def between_the_markers(existing: str, block: str) -> str:
    """Put the block back where warden's last one was, and nowhere else.

    A hosts file is somebody else's file with warden's few lines in it. The
    same rule `warden firewall adopt` follows for another program's output:
    find what is ours, replace only that, and leave the rest exactly as it is.
    """
    lines = existing.splitlines()
    try:
        start = lines.index(BEGIN)
        end = lines.index(END, start)
    except ValueError:
        kept = [line for line in lines if line.strip()]
        return "\n".join([*kept, "", *block.splitlines()]).rstrip("\n") + "\n"
    return "\n".join([*lines[:start], *block.splitlines(), *lines[end + 1 :]]).rstrip("\n") + "\n"


def render(
    shape: str,
    services: list[Registration] | list[FleetRegistration],
    *,
    node: str,
    nodes: list[Node] | None = None,
    domain: str | None = None,
) -> str:
    """One proxy's worth of configuration, ready to be redirected into a file."""
    ordered = sorted(services, key=lambda service: service.name)
    # The hosts block carries its own header between the markers, so a rewrite
    # replaces the explanation along with the lines it explains.
    lines = [] if shape == HOSTS else [f"{COMMENT[shape]} {HEADER.format(node=node)}", ""]
    lines += BUILDERS[shape](ordered, node_hosts(nodes or []), domain)
    return "\n".join(lines).rstrip("\n").replace(NODE_HERE, node) + "\n"

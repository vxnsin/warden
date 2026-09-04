"""The proxy configuration warden already knows enough to write.

Nothing here touches a file or reloads anything. It writes to stdout and stops,
because where the configuration belongs and when the proxy should pick it up
are decisions this program has no business making.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from warden.models import FleetRegistration, Node, Registration

CADDY = "caddy"
NGINX = "nginx"
TRAEFIK = "traefik"

FORMATS = (CADDY, NGINX, TRAEFIK)

# Deliberately without a timestamp. This output belongs in a repository, and a
# header that changes every run turns every regeneration into a diff.
HEADER = "Written by `warden export` from the warden on {node}. Regenerate it; do not edit it."


def hostname(service: Registration, domain: str | None) -> str:
    """What the world outside should call this service.

    A service that carries its own `domain` in its metadata means it, whatever
    anyone passed on the command line.
    """
    own = service.meta.get("domain")
    if own:
        return own
    return f"{service.name}.{domain}" if domain else service.name


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


BUILDERS = {CADDY: _caddy, NGINX: _nginx, TRAEFIK: _traefik}

COMMENT = {CADDY: "#", NGINX: "#", TRAEFIK: "#"}


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
    lines = [f"{COMMENT[shape]} {HEADER.format(node=node)}", ""]
    lines += BUILDERS[shape](ordered, node_hosts(nodes or []), domain)
    return "\n".join(lines).rstrip("\n") + "\n"

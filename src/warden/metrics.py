"""The Prometheus text format, written out by hand.

Twenty lines and no dependency, against a format that has not changed in years.
"""

from __future__ import annotations

from collections import Counter

from warden.models import Node, PoolStatus, Registration

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**pairs: str) -> str:
    return ",".join(f'{key}="{_escape(value)}"' for key, value in pairs.items())


def _metric(name: str, help_text: str, kind: str, samples: list[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", *samples]


def render(
    *,
    pool: PoolStatus,
    services: list[Registration],
    nodes: list[Node],
    version: str,
    node: str,
    role: str,
) -> str:
    """Everything a scrape asks for, from what the warden already knows.

    Deliberately nothing that needs a syscall: a scrape happens every fifteen
    seconds, and a sweep of every socket on the machine at that rate would cost
    more than the numbers are worth.
    """
    by_kind = Counter(service.kind for service in services)
    by_status = Counter(node.status for node in nodes)

    lines: list[str] = []
    lines += _metric(
        "warden_info",
        "Version, name and role of this warden.",
        "gauge",
        [f"warden_info{{{_labels(version=version, node=node, role=role)}}} 1"],
    )
    lines += _metric(
        "warden_pool_ports",
        "Ports in the range this warden hands out.",
        "gauge",
        [f"warden_pool_ports {pool.size}"],
    )
    lines += _metric(
        "warden_pool_allocated",
        "Ports currently held by a registered service.",
        "gauge",
        [f"warden_pool_allocated {pool.allocated}"],
    )
    lines += _metric(
        "warden_pool_available",
        "Ports left to hand out.",
        "gauge",
        [f"warden_pool_available {pool.available}"],
    )
    lines += _metric(
        "warden_pool_largest_run",
        "Longest stretch of free ports in a row, which is what a contiguous request needs.",
        "gauge",
        [f"warden_pool_largest_run {pool.largest_run}"],
    )
    lines += _metric(
        "warden_pool_reserved",
        "Ports this warden will never hand out.",
        "gauge",
        [f"warden_pool_reserved {len(pool.reserved)}"],
    )
    lines += _metric(
        "warden_services",
        "Registered services, by kind.",
        "gauge",
        [
            f"warden_services{{{_labels(kind=kind)}}} {count}"
            for kind, count in sorted(by_kind.items())
        ]
        or ["warden_services 0"],
    )
    lines += _metric(
        "warden_nodes",
        "Other wardens reporting to this one, by status.",
        "gauge",
        [
            f"warden_nodes{{{_labels(status=state)}}} {count}"
            for state, count in sorted(by_status.items())
        ]
        or ["warden_nodes 0"],
    )
    return "\n".join(lines) + "\n"

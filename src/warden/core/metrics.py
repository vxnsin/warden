"""The Prometheus text format, written out by hand.

Twenty lines and no dependency, against a format that has not changed in years.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

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
    walls: Walls | None = None,
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
    lines += _firewall(walls)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Walls:
    """What this warden's firewall looks like, in numbers a scrape can read.

    Counted from the store rather than from the kernel. `/metrics` is scraped
    every fifteen seconds and will not spend a syscall on it; whether the
    kernel agrees with the book is what `warden firewall pending` is for, and
    that number is here too.

    The two totals come from a tally of their own rather than from the history,
    which is capped: a counter that quietly starts again is a counter that lies.
    """

    by_origin: dict[str, int]
    live: int
    pending: int
    rollback_armed: bool
    applied: int
    rolled_back: int


def _firewall(walls: Walls | None) -> list[str]:
    if walls is None:
        return []
    lines = _metric(
        "warden_firewall_rules",
        "Firewall rules this warden holds, by where they came from.",
        "gauge",
        [
            f"warden_firewall_rules{{{_labels(origin=origin)}}} {count}"
            for origin, count in sorted(walls.by_origin.items())
        ]
        or ["warden_firewall_rules 0"],
    )
    lines += _metric(
        "warden_firewall_rules_live",
        "Rules that are in force now, rather than expired or switched off.",
        "gauge",
        [f"warden_firewall_rules_live {walls.live}"],
    )
    lines += _metric(
        "warden_firewall_pending",
        "Rules written down and not applied, and applied and no longer written down.",
        "gauge",
        [f"warden_firewall_pending {walls.pending}"],
    )
    lines += _metric(
        "warden_firewall_rollback_armed",
        "Whether a change is waiting to undo itself for want of a confirmation.",
        "gauge",
        [f"warden_firewall_rollback_armed {int(walls.rollback_armed)}"],
    )
    lines += _metric(
        "warden_firewall_applied_total",
        "Times a ruleset has been applied on this machine, for its whole life.",
        "counter",
        [f"warden_firewall_applied_total {walls.applied}"],
    )
    lines += _metric(
        "warden_firewall_rolled_back_total",
        "Times one undid itself for want of a confirmation, for the machine's whole life.",
        "counter",
        [f"warden_firewall_rolled_back_total {walls.rolled_back}"],
    )
    return lines

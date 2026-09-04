from __future__ import annotations

import socket
from collections.abc import Collection, Iterable, Iterator


def is_bound(host: str, port: int) -> bool:
    """Whether something on the host already occupies ``host:port``.

    The registry only knows what was handed out through it; a service started by
    hand would otherwise get its port handed out a second time.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    for family, socktype, proto, _canonname, address in infos:
        with socket.socket(family, socktype, proto) as sock:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            try:
                sock.bind(address)
            except OSError:
                return True
    return False


class PortPool:
    """The range of ports the registry may hand out."""

    def __init__(self, start: int, end: int, reserved: Iterable[int] = ()) -> None:
        if not 1 <= start <= end <= 65535:
            raise ValueError(f"invalid port range {start}-{end}")
        self.start = start
        self.end = end
        self.reserved = frozenset(reserved)

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def __contains__(self, port: int) -> bool:
        return self.start <= port <= self.end and port not in self.reserved

    def candidates(self, taken: Collection[int]) -> Iterator[int]:
        for port in range(self.start, self.end + 1):
            if port in self.reserved or port in taken:
                continue
            yield port

    def largest_run(self, taken: Collection[int]) -> int:
        """The longest stretch of free ports in a row this pool still has.

        A pool with forty free ports scattered one at a time cannot serve a
        request for four in a row, and saying only "forty free" hides that.
        """
        longest = run = 0
        previous: int | None = None
        for port in self.candidates(taken):
            run = run + 1 if previous is not None and port == previous + 1 else 1
            previous = port
            longest = max(longest, run)
        return longest

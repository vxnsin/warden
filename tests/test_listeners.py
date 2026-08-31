import os
import socket
import subprocess
import sys
from collections.abc import Iterator

import psutil
import pytest

from warden.errors import ProtectedProcessError, UnknownProcessError
from warden.listeners import SYSTEM_PIDS, holder_of, listeners, stop


@pytest.fixture
def bound_port() -> Iterator[int]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock.getsockname()[1]


def _unused_pid() -> int:
    taken = set(psutil.pids())
    candidate = max(taken) + 1000
    while candidate in taken or psutil.pid_exists(candidate):
        candidate += 1
    return candidate


def test_a_socket_we_opened_ourselves_shows_up(bound_port: int):
    assert bound_port in {row.port for row in listeners()}


def test_our_own_socket_is_traced_back_to_this_process(bound_port: int):
    holder = holder_of(bound_port)
    assert holder is not None
    assert holder.pid == os.getpid()
    assert holder.protocol.startswith("tcp")
    assert holder.address == f"127.0.0.1:{bound_port}"


def test_a_free_port_has_no_holder():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    assert holder_of(free) is None


def test_udp_sockets_can_be_left_out():
    assert all(row.protocol.startswith("tcp") for row in listeners(udp=False))


def test_the_listing_is_ordered_by_port():
    ports = [row.port for row in listeners()]
    assert ports == sorted(ports)


def test_every_row_names_its_protocol():
    assert {row.protocol for row in listeners()} <= {"tcp", "tcp6", "udp", "udp6"}


def test_the_operating_system_is_out_of_reach():
    for pid in sorted(SYSTEM_PIDS):
        with pytest.raises(ProtectedProcessError, match="operating system"):
            stop(pid)


def test_warden_refuses_to_stop_itself():
    with pytest.raises(ProtectedProcessError, match="warden itself"):
        stop(os.getpid())


def test_stopping_a_process_that_is_not_there_says_so():
    with pytest.raises(UnknownProcessError, match="no process is running"):
        stop(_unused_pid())


def test_a_child_process_really_does_stop():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        name = stop(child.pid, timeout=10)
        assert name
        child.wait(timeout=10)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

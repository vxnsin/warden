import socket

import pytest

from warden.ports.allocator import PortPool, is_bound


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_rejects_an_inverted_range():
    with pytest.raises(ValueError, match="invalid port range"):
        PortPool(9000, 8000)


def test_candidates_skip_reserved_and_taken():
    pool = PortPool(8000, 8005, reserved={8001, 8002})
    assert list(pool.candidates(taken={8000})) == [8003, 8004, 8005]


def test_membership_excludes_reserved_ports():
    pool = PortPool(8000, 8005, reserved={8003})
    assert 8000 in pool
    assert 8003 not in pool
    assert 9000 not in pool


def test_size_counts_both_ends():
    assert PortPool(8000, 8005).size == 6


def test_a_listening_socket_is_detected():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        assert is_bound("127.0.0.1", sock.getsockname()[1]) is True


def test_an_unused_port_is_reported_free():
    assert is_bound("127.0.0.1", _free_port()) is False

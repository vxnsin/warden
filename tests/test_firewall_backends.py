"""The registry that means a new backend is a new file and nothing else."""

import pytest

from warden.errors import NotPermittedError
from warden.firewall import backends
from warden.firewall.backends.base import KNOWN, Backend, backend_for


def test_every_backend_in_the_folder_registers_itself():
    backends.load()
    assert "nftables" in KNOWN


def test_the_base_class_does_not_register_itself_as_a_backend():
    assert "firewall" not in KNOWN


def test_a_new_backend_is_known_the_moment_it_is_defined():
    class Pretend(Backend):
        kind = "pretend"
        systems = ("Pretendix",)

    try:
        assert KNOWN["pretend"] is Pretend
        assert backend_for(system="Pretendix").kind == "pretend"
    finally:
        KNOWN.pop("pretend", None)


def test_the_machine_decides_when_nobody_names_one():
    assert backend_for(system="Linux").kind == "nftables"


def test_a_system_with_no_backend_says_what_it_does_know():
    with pytest.raises(NotPermittedError, match="no firewall backend for Plan9"):
        backend_for(system="Plan9")


def test_a_backend_asked_for_by_name_that_does_not_exist_is_refused():
    with pytest.raises(NotPermittedError, match="no firewall backend called 'pf'"):
        backend_for("pf")


def test_a_named_backend_wins_over_the_machine():
    assert backend_for("nftables", system="Plan9").kind == "nftables"


def test_the_base_class_refuses_to_pretend_it_can_do_anything():
    for doing in ("available", "render", "apply", "snapshot"):
        with pytest.raises(NotImplementedError):
            getattr(Backend(), doing)(*([None] if doing in ("render", "apply") else []))

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from warden.allocator import PortPool
from warden.errors import PoolExhaustedError, PortUnavailableError, UnknownServiceError
from warden.models import HeartbeatRequest, RegistrationRequest
from warden.service import Registry


def request(name: str, **kwargs) -> RegistrationRequest:
    return RegistrationRequest(name=name, kind=kwargs.pop("kind", "backend"), **kwargs)


def test_first_service_gets_the_lowest_port(manager: Registry):
    registration, created = manager.register(request("api"))
    assert (registration.port, created) == (8000, True)


def test_second_service_gets_the_next_port(manager: Registry):
    manager.register(request("api"))
    registration, _ = manager.register(request("web", kind="frontend"))
    assert registration.port == 8001


def test_a_restart_keeps_the_same_port(manager: Registry):
    first, _ = manager.register(request("api"))
    manager.register(request("web", kind="frontend"))
    second, created = manager.register(request("api", pid=4242))
    assert second.port == first.port
    assert created is False
    assert second.pid == 4242
    assert second.created_at == first.created_at


def test_a_released_port_is_handed_out_again(manager: Registry):
    manager.register(request("api"))
    manager.release("api")
    registration, _ = manager.register(request("web", kind="frontend"))
    assert registration.port == 8000


def test_a_preferred_port_is_honoured(manager: Registry):
    registration, _ = manager.register(request("api", preferred_port=8003))
    assert registration.port == 8003


def test_a_required_port_may_sit_outside_the_pool(manager: Registry):
    registration, _ = manager.register(request("legacy", require_port=3000))
    assert registration.port == 3000


def test_a_preferred_port_that_is_taken_falls_back_to_the_pool(manager: Registry):
    manager.register(request("api"))
    registration, _ = manager.register(request("web", kind="frontend", preferred_port=8000))
    assert registration.port == 8001


def test_a_preferred_port_that_is_reserved_falls_back_to_the_pool(store):
    manager = Registry(store, PortPool(8000, 8004, reserved={8000}), probe=False)
    registration, _ = manager.register(request("api", preferred_port=8000))
    assert registration.port == 8001


def test_a_required_port_is_honoured(manager: Registry):
    registration, _ = manager.register(request("api", require_port=8003))
    assert registration.port == 8003


def test_a_required_port_held_by_someone_else_is_refused(manager: Registry):
    manager.register(request("api"))
    with pytest.raises(PortUnavailableError, match="held by 'api'"):
        manager.register(request("web", kind="frontend", require_port=8000))


def test_a_required_port_that_is_reserved_is_refused(store):
    manager = Registry(store, PortPool(8000, 8004, reserved={8002}), probe=False)
    with pytest.raises(PortUnavailableError, match="reserved"):
        manager.register(request("api", require_port=8002))


def test_a_wish_and_a_demand_cannot_be_combined():
    with pytest.raises(ValidationError, match="not both"):
        request("api", preferred_port=8000, require_port=8001)


def test_reserved_ports_are_skipped_when_allocating(store):
    manager = Registry(store, PortPool(8000, 8004, reserved={8000, 8001}), probe=False)
    registration, _ = manager.register(request("api"))
    assert registration.port == 8002


def test_an_exhausted_pool_is_reported(manager: Registry):
    for index in range(5):
        manager.register(request(f"service-{index}"))
    with pytest.raises(PoolExhaustedError, match="8000-8004"):
        manager.register(request("one-too-many"))


def test_the_same_port_can_be_used_on_another_host(manager: Registry):
    manager.register(request("api"))
    registration, _ = manager.register(request("remote-api", host="10.0.0.5"))
    assert registration.port == 8000


def test_looking_up_an_unknown_service_fails(manager: Registry):
    with pytest.raises(UnknownServiceError, match="no service registered"):
        manager.get("nothing")


def test_releasing_twice_fails(manager: Registry):
    manager.register(request("api"))
    manager.release("api")
    with pytest.raises(UnknownServiceError):
        manager.release("api")


def test_a_heartbeat_extends_the_lease(manager: Registry):
    registration, _ = manager.register(request("api", ttl=60))
    renewed = manager.heartbeat("api", HeartbeatRequest(ttl=120))
    assert renewed.expires_at > registration.expires_at
    assert renewed.port == registration.port


def test_an_expired_registration_frees_its_port(manager: Registry):
    registration, _ = manager.register(request("api", ttl=60))
    expired = registration.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    manager.store.save(expired)
    assert manager.list() == []
    replacement, _ = manager.register(request("web", kind="frontend"))
    assert replacement.port == 8000


def test_pool_status_counts_only_ports_inside_the_pool(manager: Registry):
    manager.register(request("api"))
    manager.register(request("legacy", require_port=3000))
    status = manager.pool_status()
    assert (status.size, status.allocated, status.available) == (5, 1, 4)


def test_a_heartbeat_without_a_ttl_renews_the_original_lease(manager: Registry):
    manager.register(request("api", ttl=60))
    renewed = manager.heartbeat("api", HeartbeatRequest())
    assert renewed.ttl == 60
    assert renewed.expires_at is not None


def test_a_heartbeat_can_shorten_a_lease(manager: Registry):
    manager.register(request("api", ttl=600))
    renewed = manager.heartbeat("api", HeartbeatRequest(ttl=30))
    assert renewed.ttl == 30


def test_a_registration_without_a_ttl_never_expires(manager: Registry):
    registration, _ = manager.register(request("api"))
    assert registration.ttl is None
    assert manager.heartbeat("api", HeartbeatRequest()).expires_at is None

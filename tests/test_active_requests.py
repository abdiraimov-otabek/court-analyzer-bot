from src.domain.value_objects import UserId
from src.services.active_requests import ActiveRequestRegistry


def test_active_request_registry_tracks_one_active_request_per_user():
    registry = ActiveRequestRegistry()
    user_id = UserId("123")

    assert registry.start(user_id, query_text="q1", total_cases=10) is True
    assert registry.start(user_id, query_text="q2", total_cases=10) is False

    active = registry.get(user_id)
    assert active is not None
    assert active.query_text == "q1"
    assert active.total_cases == 10
    assert active.processed_cases == 0

    registry.finish(user_id)
    assert registry.get(user_id) is None
    assert registry.start(user_id, query_text="q3", total_cases=5) is True

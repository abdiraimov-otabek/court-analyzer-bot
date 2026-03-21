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


def test_active_request_registry_reuses_running_job_for_same_request_id():
    registry = ActiveRequestRegistry()
    user_id = UserId("123")

    first = registry.start_or_reuse(
        user_id,
        request_id="req-1",
        query_text="q1",
        total_cases=10,
        phase="queued",
    )
    second = registry.start_or_reuse(
        user_id,
        request_id="req-1",
        query_text="q1",
        total_cases=10,
        phase="queued",
    )

    assert first.status == "started"
    assert second.status == "duplicate"
    assert second.active_request is not None
    assert second.active_request.request_id == "req-1"


def test_active_request_registry_rejects_different_request_when_one_is_running():
    registry = ActiveRequestRegistry()
    user_id = UserId("123")

    registry.start_or_reuse(
        user_id,
        request_id="req-1",
        query_text="q1",
        total_cases=10,
        phase="queued",
    )
    second = registry.start_or_reuse(
        user_id,
        request_id="req-2",
        query_text="q2",
        total_cases=10,
        phase="queued",
    )

    assert second.status == "busy"


def test_active_request_registry_expands_total_when_attempted_exceeds_estimate():
    registry = ActiveRequestRegistry()
    user_id = UserId("123")
    registry.start(user_id, query_text="q1", total_cases=11)

    registry.update_attempted(user_id, attempted_cases=206)

    active = registry.get(user_id)
    assert active is not None
    assert active.total_cases == 206
    assert active.processed_cases == 206

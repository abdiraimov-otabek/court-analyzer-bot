from datetime import datetime, timedelta

from src.domain.value_objects import UserId
from src.services.rate_limit import HourlyRateLimiter


def test_rate_limiter_allows_up_to_limit():
    limiter = HourlyRateLimiter(limit=10)
    user_id = UserId("123")
    now = datetime(2026, 2, 11, 12, 0, 0)

    for _ in range(10):
        assert limiter.allow(user_id, now) is True

    assert limiter.allow(user_id, now) is False


def test_rate_limiter_resets_after_window():
    limiter = HourlyRateLimiter(limit=1)
    user_id = UserId("123")
    now = datetime(2026, 2, 11, 12, 0, 0)

    assert limiter.allow(user_id, now) is True
    assert limiter.allow(user_id, now) is False

    later = now + timedelta(hours=1, seconds=1)
    assert limiter.allow(user_id, later) is True

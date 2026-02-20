from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import threading

from src.domain.value_objects import UserId


class HourlyRateLimiter:
    def __init__(self, limit: int = 10, window_seconds: int = 3600) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._events: dict[UserId, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, user_id: UserId, now: datetime) -> bool:
        ts = now.timestamp()
        with self._lock:
            events = self._events[user_id]
            cutoff = ts - self._window_seconds
            while events and events[0] <= cutoff:
                events.pop(0)
            if len(events) >= self._limit:
                return False
            events.append(ts)
            return True

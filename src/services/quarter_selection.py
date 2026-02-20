from __future__ import annotations

from dataclasses import dataclass
import threading

from src.domain.value_objects import UserId


@dataclass(frozen=True)
class PendingQuarterSelection:
    base_query: str


class QuarterSelectionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[UserId, PendingQuarterSelection] = {}

    def set_pending(self, user_id: UserId, base_query: str) -> None:
        with self._lock:
            self._pending[user_id] = PendingQuarterSelection(base_query=base_query)

    def get_pending(self, user_id: UserId) -> PendingQuarterSelection | None:
        with self._lock:
            return self._pending.get(user_id)

    def clear_pending(self, user_id: UserId) -> None:
        with self._lock:
            self._pending.pop(user_id, None)

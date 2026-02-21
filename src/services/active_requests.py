import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from src.domain.value_objects import UserId


@dataclass
class ActiveRequest:
    user_id: UserId
    query_text: str
    total_cases: int
    started_at: datetime = field(default_factory=datetime.now)
    processed_cases: int = 0
    collected_cases: int = 0
    attempted_cases: int = 0
    successful_cases: int = 0
    retry_count: int = 0
    cancelled: bool = False
    phase: str = "counting"


class ActiveRequestRegistry:
    def __init__(self, db_repo=None) -> None:
        self._lock = threading.Lock()
        self._active: dict[UserId, ActiveRequest] = {}
        self._db_repo = db_repo
        self._logger = logging.getLogger("active_requests")

    def start(
        self,
        user_id: UserId,
        query_text: str,
        total_cases: int,
        phase: str = "counting",
    ) -> bool:
        with self._lock:
            if user_id in self._active:
                return False
            self._active[user_id] = ActiveRequest(
                user_id=user_id,
                query_text=query_text,
                total_cases=total_cases,
                phase=phase,
            )
        if self._db_repo is not None:
            try:
                self._db_repo.upsert(str(user_id.value), query_text, phase, total_cases)
            except Exception:
                pass
        return True

    def update_query_text(self, user_id: UserId, query_text: str) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.query_text = query_text

    def finish(self, user_id: UserId) -> None:
        with self._lock:
            self._active.pop(user_id, None)
        if self._db_repo is not None:
            try:
                self._db_repo.delete(str(user_id.value))
            except Exception:
                pass

    def get(self, user_id: UserId) -> ActiveRequest | None:
        with self._lock:
            return self._active.get(user_id)

    def list_all(self) -> list[ActiveRequest]:
        with self._lock:
            return list(self._active.values())

    def update_processed(self, user_id: UserId, processed_cases: int) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.processed_cases = processed_cases
            active.attempted_cases = processed_cases

    def update_collected(self, user_id: UserId, collected_cases: int) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.collected_cases = collected_cases

    def update_attempted(self, user_id: UserId, attempted_cases: int) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.attempted_cases = attempted_cases
            active.processed_cases = attempted_cases

    def update_successful(self, user_id: UserId, successful_cases: int) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.successful_cases = successful_cases

    def update_retry_count(self, user_id: UserId, retry_count: int) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.retry_count = retry_count

    def update_total_cases(self, user_id: UserId, total_cases: int) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.total_cases = total_cases

    def set_phase(self, user_id: UserId, phase: str) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.phase = phase
        if self._db_repo is not None:
            try:
                self._db_repo.update_phase(str(user_id.value), phase)
            except Exception:
                pass

    def cancel(self, user_id: UserId) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is not None:
                active.cancelled = True
        if self._db_repo is not None:
            self._db_repo.set_cancelled(str(user_id.value), True)

    def is_cancelled(self, user_id: UserId) -> bool:
        with self._lock:
            active = self._active.get(user_id)
            if active is not None and active.cancelled:
                return True

        if self._db_repo is not None:
            return self._db_repo.is_cancelled(str(user_id.value))
        return False

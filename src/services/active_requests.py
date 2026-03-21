import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from src.domain.value_objects import UserId


@dataclass(frozen=True)
class RequestStartResult:
    status: str
    active_request: "ActiveRequest | None" = None


@dataclass
class ActiveRequest:
    user_id: UserId
    query_text: str
    total_cases: int
    request_id: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    processed_cases: int = 0
    collected_cases: int = 0
    attempted_cases: int = 0
    successful_cases: int = 0
    retry_count: int = 0
    cancelled: bool = False
    phase: str = "queued"


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
        phase: str = "queued",
    ) -> bool:
        result = self.start_or_reuse(
            user_id=user_id,
            request_id="",
            query_text=query_text,
            total_cases=total_cases,
            phase=phase,
        )
        return result.status == "started"

    def start_or_reuse(
        self,
        user_id: UserId,
        request_id: str,
        query_text: str,
        total_cases: int,
        phase: str = "queued",
    ) -> RequestStartResult:
        with self._lock:
            existing = self._active.get(user_id)
            if existing is not None:
                if request_id and existing.request_id == request_id:
                    return RequestStartResult(
                        status="duplicate", active_request=existing
                    )
                return RequestStartResult(status="busy", active_request=existing)
            active = ActiveRequest(
                user_id=user_id,
                query_text=query_text,
                total_cases=total_cases,
                request_id=request_id,
                phase=phase,
            )
            self._active[user_id] = active
        if self._db_repo is not None:
            try:
                self._db_repo.upsert(
                    str(user_id.value), request_id, query_text, phase, total_cases
                )
            except Exception:
                pass
        return RequestStartResult(status="started", active_request=active)

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
            active = self._active.get(user_id)
        if active is not None or self._db_repo is None:
            return active
        row = self._db_repo.get(str(user_id.value))
        return self._deserialize(row)

    def list_all(self) -> list[ActiveRequest]:
        with self._lock:
            active = list(self._active.values())
        if self._db_repo is None:
            return active
        persisted = {
            record.user_id.value: record
            for record in map(self._deserialize, self._db_repo.list_all())
            if record is not None
        }
        for record in active:
            persisted[record.user_id.value] = record
        return list(persisted.values())

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
            if attempted_cases > active.total_cases:
                active.total_cases = attempted_cases

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

    def _deserialize(self, row) -> ActiveRequest | None:
        if not row:
            return None
        return ActiveRequest(
            user_id=UserId(str(row["user_id"])),
            request_id=row.get("request_id", ""),
            query_text=row["query_text"],
            total_cases=int(row.get("total_cases", 0)),
            phase=row.get("phase", "queued"),
            cancelled=bool(row.get("cancelled", False)),
        )

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from src.domain.entities import CaseDecision, CaseOutcome
from src.infrastructure.sqlite import SqliteConnection


class CaseDetailsCacheRepository:
    _SCHEMA_VERSION = "v8"

    def __init__(self, connection: SqliteConnection, ttl_seconds: int = 24 * 60 * 60) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._connection = connection
        self._ttl = timedelta(seconds=ttl_seconds)

    def get(self, case_id: str, now: datetime) -> CaseDecision | None:
        self._cleanup(now)
        with self._connection.connect() as conn:
            row = conn.execute(
                "select payload, expires_at from case_details_cache where case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= now:
            self.delete(case_id)
            return None
        decision = self._deserialize_decision(row["payload"])
        if decision is None:
            self.delete(case_id)
        return decision

    def set(
        self,
        case_id: str,
        decision: CaseDecision,
        now: datetime,
        ttl_seconds: int | None = None,
    ) -> None:
        payload = self._serialize_decision(decision)
        ttl = self._ttl if ttl_seconds is None else timedelta(seconds=ttl_seconds)
        expires_at = now + ttl
        with self._connection.connect() as conn:
            conn.execute(
                """
                insert into case_details_cache (case_id, payload, created_at, expires_at)
                values (?, ?, ?, ?)
                on conflict(case_id) do update set
                    payload=excluded.payload,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at
                """,
                (case_id, payload, now.isoformat(), expires_at.isoformat()),
            )
            conn.commit()

    def delete(self, case_id: str) -> None:
        with self._connection.connect() as conn:
            conn.execute("delete from case_details_cache where case_id = ?", (case_id,))
            conn.commit()

    def _cleanup(self, now: datetime) -> None:
        with self._connection.connect() as conn:
            conn.execute("delete from case_details_cache where expires_at <= ?", (now.isoformat(),))
            conn.commit()

    def _serialize_decision(self, decision: CaseDecision) -> str:
        return json.dumps(
            {
                "schema_version": self._SCHEMA_VERSION,
                "case_number": decision.case_number,
                "decision_date": decision.decision_date.isoformat(),
                "outcome": decision.outcome.value,
                "reasons": decision.reasons,
                "case_id": decision.case_id,
                "court_name": decision.court_name,
                "case_link": decision.case_link,
                "analysis_text": decision.analysis_text,
                "case_category": decision.case_category,
            },
            ensure_ascii=False,
        )

    def _deserialize_decision(self, payload: str) -> CaseDecision | None:
        data = json.loads(payload)
        if data.get("schema_version") != self._SCHEMA_VERSION:
            return None
        return CaseDecision(
            case_number=data["case_number"],
            decision_date=date.fromisoformat(data["decision_date"]),
            outcome=CaseOutcome(data["outcome"]),
            reasons=tuple(data.get("reasons", [])),
            case_id=str(data.get("case_id", "") or ""),
            court_name=str(data.get("court_name", "") or ""),
            case_link=str(data.get("case_link", "") or ""),
            analysis_text=str(data.get("analysis_text", "") or ""),
            case_category=str(data.get("case_category", "") or ""),
        )

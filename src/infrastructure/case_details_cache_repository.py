from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta

from src.domain.entities import CaseDecision, CaseOutcome
from src.infrastructure.sqlite import SqliteConnection


class CaseDetailsCacheRepository:
    _SCHEMA_VERSION = "v12"

    def __init__(
        self, connection: SqliteConnection, ttl_seconds: int = 24 * 60 * 60
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._connection = connection
        self._last_cleanup_at = 0.0
        self._ttl = timedelta(seconds=ttl_seconds)
        self._disabled = False
        self._logger = logging.getLogger("case_details_cache_repository")

    def get(self, case_id: str, now: datetime) -> CaseDecision | None:
        if self._disabled:
            return None
        t_now = time.time()
        if t_now - self._last_cleanup_at > 60:
            self._cleanup(now)
            self._last_cleanup_at = t_now
        try:
            with self._connection.connect() as conn:
                row = conn.execute(
                    "select payload, expires_at from case_details_cache where case_id = ?",
                    (case_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            if self._disable_on_corruption(exc):
                return None
            raise
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
        if self._disabled:
            return
        payload = self._serialize_decision(decision)
        ttl = self._ttl if ttl_seconds is None else timedelta(seconds=ttl_seconds)
        expires_at = now + ttl
        try:
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
        except sqlite3.DatabaseError as exc:
            if self._disable_on_corruption(exc):
                return
            raise

    def delete(self, case_id: str) -> None:
        if self._disabled:
            return
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    "delete from case_details_cache where case_id = ?", (case_id,)
                )
        except sqlite3.DatabaseError as exc:
            if self._disable_on_corruption(exc):
                return
            raise

    def _cleanup(self, now: datetime) -> None:
        if self._disabled:
            return
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    "delete from case_details_cache where expires_at <= ?",
                    (now.isoformat(),),
                )
        except sqlite3.DatabaseError as exc:
            if self._disable_on_corruption(exc):
                return
            raise

    def _disable_on_corruption(self, exc: sqlite3.DatabaseError) -> bool:
        message = str(exc).lower()
        corruption_markers = (
            "database disk image is malformed",
            "malformed",
            "file is not a database",
        )
        if not any(marker in message for marker in corruption_markers):
            return False
        if not self._disabled:
            self._logger.warning(
                "case_details_cache.disabled_due_to_corruption",
                extra={"error": str(exc)},
            )
        self._disabled = True
        return True

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
                "document_links": decision.document_links,
                "proof_quote": decision.proof_quote,
                "reason_confidence": decision.reason_confidence,
                "matched_article": decision.matched_article,
                "evidence_quote": decision.evidence_quote,
                "decisive_act_title": decision.decisive_act_title,
                "decisive_act_url": decision.decisive_act_url,
                "decisive_act_type": decision.decisive_act_type,
                "pdf_status": decision.pdf_status,
                "verification_failure_code": decision.verification_failure_code,
                "law_display_name": decision.law_display_name,
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
            document_links=tuple(data.get("document_links", []) or []),
            proof_quote=str(data.get("proof_quote", "") or ""),
            reason_confidence=float(data.get("reason_confidence", 1.0)),
            matched_article=str(data.get("matched_article", "") or ""),
            evidence_quote=str(data.get("evidence_quote", "") or ""),
            decisive_act_title=str(data.get("decisive_act_title", "") or ""),
            decisive_act_url=str(data.get("decisive_act_url", "") or ""),
            decisive_act_type=str(data.get("decisive_act_type", "") or ""),
            pdf_status=str(data.get("pdf_status", "not_requested") or "not_requested"),
            verification_failure_code=str(data.get("verification_failure_code", "") or ""),
            law_display_name=str(data.get("law_display_name", "") or ""),
        )

from datetime import date, datetime, timedelta

from src.domain.entities import CaseDecision, CaseOutcome
from src.infrastructure.case_details_cache_repository import CaseDetailsCacheRepository
from src.infrastructure.sqlite import SqliteConnection


def test_case_details_cache_roundtrip_and_expiry(tmp_path):
    repo = CaseDetailsCacheRepository(SqliteConnection(str(tmp_path / "app.db")), ttl_seconds=60)
    now = datetime(2026, 2, 11, 12, 0, 0)
    decision = CaseDecision(
        case_number="A40-1/2023",
        decision_date=date(2023, 3, 15),
        outcome=CaseOutcome.SATISFIED,
        reasons=("доказан вред",),
    )

    repo.set("case-1", decision, now)
    loaded = repo.get("case-1", now)
    assert loaded is not None
    assert loaded.case_number == "A40-1/2023"
    assert loaded.outcome == CaseOutcome.SATISFIED

    expired = repo.get("case-1", now + timedelta(seconds=61))
    assert expired is None

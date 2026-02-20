from datetime import datetime, timedelta

from src.infrastructure.cache_repository import AnalysisCacheRepository
from src.infrastructure.sqlite import SqliteConnection
from src.domain.entities import AnalysisResult


def test_cache_repository_expires_values(tmp_path):
    db_path = tmp_path / "app.db"
    repo = AnalysisCacheRepository(SqliteConnection(str(db_path)), ttl_seconds=60)
    now = datetime(2026, 2, 11, 12, 0, 0)

    repo.set("key", AnalysisResult(summary="s", case_list="c"), now)

    assert repo.get("key", now) is not None

    later = now + timedelta(seconds=61)
    assert repo.get("key", later) is None

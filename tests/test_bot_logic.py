from datetime import datetime

from src.core.bot_logic import BotLogic
from src.core.estimation import estimate_minutes
from src.domain.settings import Settings
from src.domain.value_objects import UserId
from src.services.access_control import AccessControlList, InMemoryAccessStore
from src.services.active_requests import ActiveRequestRegistry
from src.services.quarter_selection import QuarterSelectionRegistry
from src.services.rate_limit import HourlyRateLimiter

VALID_QUERY = "Практика по статье 61.2 в АС Москвы за 2024 год"
VALID_QUERY_2 = "Практика по статье 61.3 в АС Москвы за 2024 год"


class FakeCountProvider:
    def __init__(self, mapping: dict[str, int], default_count: int = 42):
        self._mapping = mapping
        self._default = default_count

    def count_cases(self, query_text: str, _settings: Settings) -> int:
        return self._mapping.get(query_text, self._default)


class FakeSettingsProvider:
    def __init__(self, max_cases: int = 2000, allow_all_users: bool = False) -> None:
        self._settings = Settings(
            max_cases=max_cases,
            max_documents_per_case=5,
            max_pages=80,
            fetch_concurrency_min=6,
            fetch_concurrency_max=10,
            slow_alert_minutes=5,
            details_cache_ttl_seconds=24 * 60 * 60,
            analysis_prompt="test",
            updated_at=datetime(2026, 2, 11, 12, 0, 0),
            allow_all_users=allow_all_users,
        )

    def get_settings(self) -> Settings:
        return self._settings


def build_logic(
    mapping: dict[str, int] | None = None, default_count: int = 42
) -> BotLogic:
    acl = AccessControlList(InMemoryAccessStore())
    user_id = UserId("123")
    acl.grant(user_id)
    return BotLogic(
        access_control=acl,
        rate_limiter=HourlyRateLimiter(limit=10),
        active_requests=ActiveRequestRegistry(),
        quarter_selections=QuarterSelectionRegistry(),
        count_provider=FakeCountProvider(mapping or {}, default_count=default_count),
        settings_provider=FakeSettingsProvider(),
        estimate_minutes=estimate_minutes,
    )


def test_bot_logic_handles_zero_results():
    logic = build_logic(mapping={VALID_QUERY: 0})
    user_id = UserId("123")

    messages = logic.handle_message(
        user_id, VALID_QUERY, datetime(2026, 2, 11, 12, 0, 0)
    )

    assert len(messages) == 2
    assert "Оцениваю" in messages[0]
    assert "не найдено" in messages[1]


def test_bot_logic_allows_small_non_zero_result_count():
    logic = build_logic(mapping={VALID_QUERY: 4})
    user_id = UserId("123")

    messages = logic.handle_message(
        user_id, VALID_QUERY, datetime(2026, 2, 11, 12, 0, 0)
    )

    assert len(messages) == 2
    assert "Оцениваю" in messages[0]
    assert "Начинаю анализ" in messages[1]


def test_bot_logic_offers_quarter_choice_for_too_many_results():
    logic = build_logic(mapping={VALID_QUERY: 2001})
    user_id = UserId("123")

    messages = logic.handle_message(
        user_id, VALID_QUERY, datetime(2026, 2, 11, 12, 0, 0)
    )

    assert len(messages) == 2
    assert "Оцениваю" in messages[0]
    assert "Выберите период" in messages[1]
    assert "1. Январь-март" in messages[1]


def test_bot_logic_starts_analysis_after_quarter_selected():
    source_query = VALID_QUERY
    quarter_query = f"{source_query} 1 квартал 2024"
    logic = build_logic(mapping={source_query: 3000, quarter_query: 120})
    user_id = UserId("123")

    first = logic.handle_message(user_id, source_query, datetime(2026, 2, 11, 12, 0, 0))
    second = logic.handle_message(user_id, "1", datetime(2026, 2, 11, 12, 0, 3))

    assert "Выберите период" in first[1]
    assert len(second) == 1
    assert "Начинаю анализ" in second[0]
    assert "120" in second[0]


def test_bot_logic_rejects_invalid_quarter_input():
    source_query = VALID_QUERY
    logic = build_logic(mapping={source_query: 3000})
    user_id = UserId("123")

    logic.handle_message(user_id, source_query, datetime(2026, 2, 11, 12, 0, 0))
    messages = logic.handle_message(user_id, "май", datetime(2026, 2, 11, 12, 0, 1))

    assert len(messages) == 1
    assert "Выберите квартал" in messages[0]


def test_bot_logic_caps_results_when_quarter_is_still_overflow():
    source_query = VALID_QUERY
    quarter_query = f"{source_query} 1 квартал 2024"
    logic = build_logic(mapping={source_query: 3000, quarter_query: 3000})
    user_id = UserId("123")

    logic.handle_message(user_id, source_query, datetime(2026, 2, 11, 12, 0, 0))
    messages = logic.handle_message(user_id, "1", datetime(2026, 2, 11, 12, 0, 3))

    assert len(messages) == 1
    assert "анализирую до 2000" in messages[0]
    assert "Начинаю анализ до 2000 дел" in messages[0]


def test_bot_logic_starts_analysis_for_valid_range():
    logic = build_logic(mapping={VALID_QUERY: 42})
    user_id = UserId("123")

    messages = logic.handle_message(
        user_id, VALID_QUERY, datetime(2026, 2, 11, 12, 0, 0)
    )

    assert len(messages) == 2
    assert "Оцениваю" in messages[0]
    assert "Начинаю анализ" in messages[1]
    assert "42" in messages[1]


def test_bot_logic_blocks_if_active_request_exists():
    logic = build_logic(mapping={VALID_QUERY: 42, VALID_QUERY_2: 42})
    user_id = UserId("123")

    logic.handle_message(user_id, VALID_QUERY, datetime(2026, 2, 11, 12, 0, 0))
    messages = logic.handle_message(
        user_id, VALID_QUERY_2, datetime(2026, 2, 11, 12, 1, 0)
    )

    assert len(messages) == 1
    assert "активный запрос" in messages[0]


def test_bot_logic_allows_user_when_global_access_enabled():
    acl = AccessControlList(InMemoryAccessStore())
    logic = BotLogic(
        access_control=acl,
        rate_limiter=HourlyRateLimiter(limit=10),
        active_requests=ActiveRequestRegistry(),
        quarter_selections=QuarterSelectionRegistry(),
        count_provider=FakeCountProvider({VALID_QUERY: 10}),
        settings_provider=FakeSettingsProvider(allow_all_users=True),
        estimate_minutes=estimate_minutes,
    )
    user_id = UserId("999")

    messages = logic.handle_message(
        user_id, VALID_QUERY, datetime(2026, 2, 11, 12, 0, 0)
    )

    assert "Начинаю анализ" in messages[-1]


def test_status_reports_collection_progress():
    acl = AccessControlList(InMemoryAccessStore())
    user_id = UserId("123")
    acl.grant(user_id)
    active_requests = ActiveRequestRegistry()
    logic = BotLogic(
        access_control=acl,
        rate_limiter=HourlyRateLimiter(limit=10),
        active_requests=active_requests,
        quarter_selections=QuarterSelectionRegistry(),
        count_provider=FakeCountProvider({}),
        settings_provider=FakeSettingsProvider(),
        estimate_minutes=estimate_minutes,
    )
    active_requests.start(
        user_id, query_text="query", total_cases=500, phase="collecting"
    )
    active_requests.update_collected(user_id, 120)

    messages = logic.handle_message(user_id, "/status", datetime(2026, 2, 11, 12, 0, 0))

    assert len(messages) == 1
    assert "подбираю дела" in messages[0]
    assert "120 из 500" in messages[0]


def test_bot_logic_requires_court_and_period_before_counting():
    # Prevent expensive and misleading searches when query misses required params.
    u = UserId("123")

    m1 = build_logic(mapping={}).handle_message(
        u, "Практика по статье 61.2 за 2024 год", datetime(2026, 2, 11, 12, 0, 0)
    )
    m2 = build_logic(mapping={}).handle_message(
        u, "Практика по статье 61.2 в АС Москвы", datetime(2026, 2, 11, 12, 0, 0)
    )

    assert len(m1) == 1
    assert "Уточните суд" in m1[0]

    assert len(m2) == 1
    assert "Уточните период" in m2[0]


def test_status_reports_attempted_and_successful_counts_when_they_differ():
    acl = AccessControlList(InMemoryAccessStore())
    user_id = UserId("777")
    acl.grant(user_id)
    active_requests = ActiveRequestRegistry()
    logic = BotLogic(
        access_control=acl,
        rate_limiter=HourlyRateLimiter(limit=10),
        active_requests=active_requests,
        quarter_selections=QuarterSelectionRegistry(),
        count_provider=FakeCountProvider({}),
        settings_provider=FakeSettingsProvider(),
        estimate_minutes=estimate_minutes,
    )
    active_requests.start(user_id, query_text="query", total_cases=400, phase="analyzing")
    active_requests.update_attempted(user_id, 400)
    active_requests.update_successful(user_id, 338)

    messages = logic.handle_message(user_id, "/status", datetime(2026, 2, 11, 12, 0, 0))

    assert len(messages) == 1
    assert "обработано 400 из 400" in messages[0]
    assert "успешно загружено 338" in messages[0]

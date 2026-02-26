from datetime import datetime

import pytest

from src.core.bot_logic import BotLogic
from src.core.estimation import estimate_minutes
from src.domain.settings import Settings
from src.domain.value_objects import UserId
from src.services.access_control import AccessControlList, InMemoryAccessStore
from src.services.active_requests import ActiveRequestRegistry
from src.services.quarter_selection import QuarterSelectionRegistry
from src.services.rate_limit import HourlyRateLimiter


class MappedCountProvider:
    def __init__(self, mapping: dict[str, int], default_count: int = 42) -> None:
        self._mapping = mapping
        self._default = default_count

    def count_cases(self, query_text: str, _settings: Settings) -> int:
        return self._mapping.get(query_text, self._default)


class StaticSettingsProvider:
    def __init__(self, max_cases: int = 2000) -> None:
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
        )

    def get_settings(self) -> Settings:
        return self._settings


def build_logic(mapping: dict[str, int]) -> BotLogic:
    acl = AccessControlList(InMemoryAccessStore())
    user_id = UserId("123")
    acl.grant(user_id)
    return BotLogic(
        access_control=acl,
        rate_limiter=HourlyRateLimiter(limit=10),
        active_requests=ActiveRequestRegistry(),
        quarter_selections=QuarterSelectionRegistry(),
        count_provider=MappedCountProvider(mapping),
        settings_provider=StaticSettingsProvider(),
        estimate_minutes=estimate_minutes,
    )


typical_queries = [
    "ст. 61.2 оспаривание сделок в 9 ААС за 2023 год",
    "ст. 61.3 оспаривание сделок в 9 ААС за 2023 год",
    "привлечение к субсидиарной ответственности ст. 61.11 в АС Москвы за 2022 год",
    "споры о включении в реестр требований кредиторов в АС Москвы за 2023 год",
    "взыскание убытков с арбитражного управляющего в АС Москвы за 2023 год",
    "жалобы на действия АУ 2022, 9 ААС",
    "оспаривание торгов в деле о банкротстве в АС Москвы за 2021 год",
    "утверждение мирового соглашения в банкротстве в АС Москвы за 2023 год",
    "распределение конкурсной массы в АС Москвы за 2022 год",
    "признание недействительным договора цессии в АС Москвы за 2023 год",
]

edge_queries = [
    "несуществующий суд 2099",
    "9 ААС 2099 год",
    "!@#$%^&*()",
    "А00-0000/0000",
    "банкротство",
]
edge_overflow_query = "слишком много дел в АС Москвы за 2023 год"

complex_queries = [
    "Анализ жалоб на бездействие АУ по оспариванию сделок в 9 ААС за 2023 год",
    "Обзор практики отказов по ст. 61.2 при оспаривании сделок по заниженной цене в АС Москвы за 2023 год",
    "Практика удовлетворения требований о привлечении к субсидиарной ответственности в АС Москвы за 2023 год",
    "Отказы в утверждении мировых соглашений в банкротстве в АС Москвы за 2023 год",
    "Оспаривание сделок должника при аффилированности сторон в АС Москвы за 2022 год",
]


@pytest.mark.parametrize("query", typical_queries + complex_queries)
def test_scenarios_start_analysis(query):
    mapping: dict[str, int] = {}
    logic = build_logic(mapping)
    user_id = UserId("123")
    messages = logic.handle_message(user_id, query, datetime(2026, 2, 11, 12, 0, 0))

    assert len(messages) == 2
    assert "Оцениваю" in messages[0]
    assert "Начинаю анализ" in messages[1]


@pytest.mark.parametrize("query", edge_queries)
def test_edge_scenarios_handle_no_results(query):
    mapping = {query: 0 for query in edge_queries}
    logic = build_logic(mapping)
    user_id = UserId("123")
    messages = logic.handle_message(user_id, query, datetime(2026, 2, 11, 12, 0, 0))

    if query == "9 ААС 2099 год":
        assert len(messages) == 2
        assert "Оцениваю" in messages[0]
        assert "не найдено" in messages[1]
    else:
        assert len(messages) == 1
        assert "уточните" in messages[0].lower() or "укажите" in messages[0].lower()


def test_edge_scenario_handles_too_many_results():
    mapping = {edge_overflow_query: 2001}
    logic = build_logic(mapping)
    user_id = UserId("123")
    messages = logic.handle_message(
        user_id, edge_overflow_query, datetime(2026, 2, 11, 12, 0, 0)
    )

    assert len(messages) == 2
    assert "Оцениваю" in messages[0]
    assert "более 2000" in messages[1]

from __future__ import annotations

import httpx

from src.domain.case_models import SearchParams
from src.services.kad_client import ParserApiKadClient


def test_build_query_keeps_case_type_for_article_queries() -> None:
    client = ParserApiKadClient(
        base_url="https://kad.arbitr.ru/",
        api_key="test",
        sync_http_client=httpx.Client(),
        async_http_client=object(),
    )
    try:
        params = SearchParams(
            inn_or_name=None,
            inn_type="Any",
            date_from="2024-01-01",
            date_to="2024-12-31",
            court="АС города Москвы",
            case_type="B",
            case_number=None,
            article="61.2",
            full_article="ст. 61.2 Закона о банкротстве",
            law_family="127-ФЗ",
            law_display_name="Закона о банкротстве",
            law_inferred=True,
            part=None,
            paragraph=None,
            subparagraph=None,
            issue_phrase=None,
        )

        query = client._build_query(params, 1)

        assert query["CaseType"] == "B"
        assert "Text" not in query
    finally:
        client._sync_http_client.close()


def test_build_query_keeps_case_type_for_non_article_queries() -> None:
    client = ParserApiKadClient(
        base_url="https://kad.arbitr.ru/",
        api_key="test",
        sync_http_client=httpx.Client(),
        async_http_client=object(),
    )
    try:
        params = SearchParams(
            inn_or_name=None,
            inn_type="Any",
            date_from="2024-01-01",
            date_to="2024-12-31",
            court="АС города Москвы",
            case_type="B",
            case_number=None,
            article=None,
            full_article=None,
            law_family=None,
            law_display_name=None,
            law_inferred=False,
            part=None,
            paragraph=None,
            subparagraph=None,
            issue_phrase=None,
        )

        query = client._build_query(params, 1)

        assert query["CaseType"] == "B"
    finally:
        client._sync_http_client.close()

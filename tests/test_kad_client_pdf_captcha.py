from __future__ import annotations

import logging
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.domain.entities import CaseDecision, CaseOutcome
from src.domain.settings import default_settings
from src.services.kad_client import ParserApiKadClient


def test_pdf_extraction_skips_kad_captcha_html() -> None:
    client = ParserApiKadClient.__new__(ParserApiKadClient)
    client._logger = logging.getLogger("test.kad_client")
    client._base_url = "https://kad.arbitr.ru"
    client._sync_http_client = MagicMock(
        get=MagicMock(
            return_value=SimpleNamespace(
                status_code=200,
                headers={"content-type": "text/html"},
                content=(
                    b"<!DOCTYPE html><html><body>"
                    b"<form id='tokenFrom'><input name='RecaptchaToken' /></form>"
                    b"<script>pravocaptcha.execute(onSubmit);</script>"
                    b"</body></html>"
                ),
            )
        )
    )
    client._current_article = "61.2"

    decision = CaseDecision(
        case_number="A40-1/2024",
        decision_date=date(2024, 1, 1),
        outcome=CaseOutcome.UNKNOWN,
        reasons=(),
        document_links=(
            {
                "name": "Определение",
                "url": "https://kad.arbitr.ru/Kad/PdfDocument/test.pdf",
            },
        ),
    )
    settings = default_settings(date(2026, 3, 28))

    text = client._extract_pdf_text_sync(decision, settings)

    assert text is None
    assert client._response_looks_like_captcha(
        "text/html",
        b"<!DOCTYPE html><form id='tokenFrom'><script>pravocaptcha.execute()</script>",
    )

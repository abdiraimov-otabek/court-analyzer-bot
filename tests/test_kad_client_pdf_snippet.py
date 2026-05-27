from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import MagicMock

from src.domain.entities import CaseOutcome
from src.domain.settings import default_settings
from src.services.kad_client import ParserApiKadClient


def test_pdf_snippet_branch_uses_passed_settings():
    client = ParserApiKadClient.__new__(ParserApiKadClient)
    client._extract_pdf_text_sync = MagicMock(return_value="PDF SNIPPET")
    client._logger = logging.getLogger("test.kad_client")

    settings = default_settings(datetime(2026, 3, 28, 12, 0, 0))
    events = [
        {
            "Date": "2024-01-01",
            "EventTypeName": "Определение",
            "Text": "Краткая карточка без подробного текста",
            "File": "https://example.com/doc.pdf",
        }
    ]

    outcome, reasons, decision_date, analysis_text, document_links, reason_conf = (
        client._extract_outcome_and_reasons(events, settings)
    )

    assert outcome == CaseOutcome.UNKNOWN
    assert reasons == ("оценка обстоятельств дела",)
    assert decision_date.year == 2024
    assert "PDF SNIPPET" in analysis_text
    assert document_links == ({"name": "Определение", "url": "https://example.com/doc.pdf"},)
    assert reason_conf == 0.1
    client._extract_pdf_text_sync.assert_called_once()
    assert client._extract_pdf_text_sync.call_args.args[1] is settings

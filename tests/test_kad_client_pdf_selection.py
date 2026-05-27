from __future__ import annotations

from src.services.kad_client import ParserApiKadClient


def test_pdf_candidate_selection_prefers_merits_text_over_procedural_text() -> None:
    client = ParserApiKadClient.__new__(ParserApiKadClient)
    client._current_article = "61.2"
    client._current_law_family = "127-ФЗ"

    procedural_doc = {
        "name": "Ходатайство об ознакомлении с материалами дела",
        "url": "https://example.com/procedural.pdf",
    }
    merits_doc = {
        "name": "Решение суда",
        "url": "https://example.com/merits.pdf",
    }

    procedural_text = "Ходатайство об ознакомлении с материалами дела"
    merits_text = (
        "Суд признал сделку недействительной по ст. 61.2 Закона о банкротстве. "
        "Установлено неравноценное встречное исполнение."
    )

    procedural_candidate = (
        procedural_doc,
        procedural_text,
        *client._score_pdf_candidate(
            procedural_doc, procedural_text, client._current_article
        ),
    )
    merits_candidate = (
        merits_doc,
        merits_text,
        *client._score_pdf_candidate(merits_doc, merits_text, client._current_article),
    )

    best_doc, best_text, best_score, best_reasons = client._select_best_pdf_candidate(
        [procedural_candidate, merits_candidate]
    )

    assert best_doc["url"] == merits_doc["url"]
    assert best_text == merits_text
    assert best_score > procedural_candidate[2]
    assert "article_match" in best_reasons

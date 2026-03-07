import asyncio
from datetime import date

from src.domain.analysis import AnalysisService
from src.domain.entities import CaseDecision, CaseOutcome


def test_analysis_service_builds_summary_and_list():
    service = AnalysisService()
    decisions = [
        CaseDecision(
            case_number="A40-1/2023",
            decision_date=date(2023, 3, 15),
            outcome=CaseOutcome.SATISFIED,
            reasons=(
                "доказан вред",
                "нарушение процедуры",
            ),
            case_id="id-1",
            court_name="9 ААС",
            case_link="https://kad.arbitr.ru/Card/id-1",
        ),
        CaseDecision(
            case_number="A40-2/2023",
            decision_date=date(2023, 4, 1),
            outcome=CaseOutcome.DENIED,
            reasons=("пропуск срока",),
            case_id="id-2",
            court_name="9 ААС",
            case_link="https://kad.arbitr.ru/Card/id-2",
        ),
        CaseDecision(
            case_number="A40-3/2023",
            decision_date=date(2023, 5, 1),
            outcome=CaseOutcome.DENIED,
            reasons=("пропуск срока",),
            case_id="id-3",
            court_name="9 ААС",
            case_link="https://kad.arbitr.ru/Card/id-3",
        ),
    ]

    result = asyncio.run(
        service.build_result(
            court="9 ААС",
            period="2023 год",
            decisions=decisions,
        )
    )

    assert "Суд: 9 ААС" in result.summary
    assert "Всего верифицировано: 3" in result.summary
    assert "Удовлетворено - 1 (33%)" in result.summary
    assert "Отказано - 2 (67%)" in result.summary
    assert "Не определено - 0 (0%)" in result.summary
    assert "Топ-2 основания для удовлетворения" in result.summary
    assert "Топ-2 основания для отказа" in result.summary

    lines = result.case_list.splitlines()
    assert "A40-1/2023 | 15.03.2023 | Удовлетворено" in lines[0]
    assert "Суд: 9 ААС" in lines[0]
    assert "Основание: доказан вред; нарушение процедуры" in lines[0]
    assert "Ссылка: https://kad.arbitr.ru/Card/id-1" in lines[0]
    assert "A40-2/2023 | 01.04.2023 | Отказано" in lines[1]
    assert "A40-3/2023 | 01.05.2023 | Отказано" in lines[2]


def test_analysis_service_top_reasons_are_distinct_between_outcomes():
    service = AnalysisService()
    decisions = [
        CaseDecision(
            case_number="A40-1/2024",
            decision_date=date(2024, 1, 10),
            outcome=CaseOutcome.SATISFIED,
            reasons=(
                "сделка с предпочтением",
                "причинение вреда кредиторам",
            ),
            case_id="x1",
            court_name="АС города Москвы",
            case_link="https://kad.arbitr.ru/Card/x1",
        ),
        CaseDecision(
            case_number="A40-2/2024",
            decision_date=date(2024, 2, 10),
            outcome=CaseOutcome.SATISFIED,
            reasons=("сделка с предпочтением",),
            case_id="x2",
            court_name="АС города Москвы",
            case_link="https://kad.arbitr.ru/Card/x2",
        ),
        CaseDecision(
            case_number="A40-3/2024",
            decision_date=date(2024, 3, 10),
            outcome=CaseOutcome.DENIED,
            reasons=(
                "сделка с предпочтением",
                "пропуск срока",
            ),
            case_id="x3",
            court_name="АС города Москвы",
            case_link="https://kad.arbitr.ru/Card/x3",
        ),
    ]

    result = asyncio.run(
        service.build_result("АС города Москвы", "2024 год", decisions)
    )

    assert (
        "Топ-2 основания для удовлетворения: причинение вреда кредиторам"
        in result.summary
    )
    assert "Топ-2 основания для отказа: пропуск срока" in result.summary


def test_analysis_service_keeps_unknown_outcomes_and_percentages_sum():
    service = AnalysisService()
    decisions = [
        CaseDecision(
            case_number="A40-10/2024",
            decision_date=date(2024, 1, 1),
            outcome=CaseOutcome.SATISFIED,
            reasons=("нарушение процедуры",),
        ),
        CaseDecision(
            case_number="A40-11/2024",
            decision_date=date(2024, 1, 2),
            outcome=CaseOutcome.DENIED,
            reasons=("пропуск срока",),
        ),
        CaseDecision(
            case_number="A40-12/2024",
            decision_date=date(2024, 1, 3),
            outcome=CaseOutcome.UNKNOWN,
            reasons=(),
        ),
    ]

    result = asyncio.run(
        service.build_result("АС города Москвы", "2024 год", decisions)
    )

    assert "Удовлетворено - 1 (33%)" in result.summary
    assert "Отказано - 1 (33%)" in result.summary
    assert "Не определено - 1 (33%)" in result.summary
    assert "Не определено" in result.case_list


def test_analysis_service_low_confidence_marker():
    """Decisions with reason_confidence < 0.5 should show [~] marker."""
    service = AnalysisService()
    decisions = [
        CaseDecision(
            case_number="A40-50/2024",
            decision_date=date(2024, 1, 1),
            outcome=CaseOutcome.SATISFIED,
            reasons=("оспаривание сделки",),
            case_id="id-50",
            court_name="АС города Москвы",
            case_link="https://kad.arbitr.ru/Card/id-50",
            reason_confidence=0.3,
        ),
    ]

    result = asyncio.run(
        service.build_result("АС города Москвы", "2024 год", decisions)
    )

    assert "[~]" in result.case_list


def test_analysis_service_high_confidence_no_marker():
    """Decisions with reason_confidence >= 0.5 should NOT show [~] marker."""
    service = AnalysisService()
    decisions = [
        CaseDecision(
            case_number="A40-51/2024",
            decision_date=date(2024, 1, 1),
            outcome=CaseOutcome.SATISFIED,
            reasons=("оспаривание сделки",),
            case_id="id-51",
            court_name="АС города Москвы",
            case_link="https://kad.arbitr.ru/Card/id-51",
            reason_confidence=0.7,
        ),
    ]

    result = asyncio.run(
        service.build_result("АС города Москвы", "2024 год", decisions)
    )

    assert "[~]" not in result.case_list


def test_analysis_service_quality_warning_low_avg_confidence():
    """Summary should include quality warning when avg reason confidence < 0.6."""
    service = AnalysisService()
    decisions = [
        CaseDecision(
            case_number="A40-60/2024",
            decision_date=date(2024, 1, 1),
            outcome=CaseOutcome.SATISFIED,
            reasons=("оспаривание сделки",),
            reason_confidence=0.3,
        ),
        CaseDecision(
            case_number="A40-61/2024",
            decision_date=date(2024, 1, 2),
            outcome=CaseOutcome.DENIED,
            reasons=("пропуск срока",),
            reason_confidence=0.4,
        ),
    ]

    result = asyncio.run(
        service.build_result("АС города Москвы", "2024 год", decisions)
    )

    assert "Низкая уверенность в основаниях" in result.summary


def test_analysis_service_no_warning_high_avg_confidence():
    """Summary should NOT include quality warning when avg reason confidence >= 0.6."""
    service = AnalysisService()
    decisions = [
        CaseDecision(
            case_number="A40-70/2024",
            decision_date=date(2024, 1, 1),
            outcome=CaseOutcome.SATISFIED,
            reasons=("оспаривание сделки",),
            reason_confidence=0.9,
        ),
        CaseDecision(
            case_number="A40-71/2024",
            decision_date=date(2024, 1, 2),
            outcome=CaseOutcome.DENIED,
            reasons=("пропуск срока",),
            reason_confidence=0.8,
        ),
    ]

    result = asyncio.run(
        service.build_result("АС города Москвы", "2024 год", decisions)
    )

    assert "Низкая уверенность в основаниях" not in result.summary


def test_analysis_service_provides_top_reasons_even_when_reason_lists_are_empty():
    service = AnalysisService()
    decisions = [
        CaseDecision(
            case_number="A40-20/2024",
            decision_date=date(2024, 1, 1),
            outcome=CaseOutcome.SATISFIED,
            reasons=(),
        ),
        CaseDecision(
            case_number="A40-21/2024",
            decision_date=date(2024, 1, 2),
            outcome=CaseOutcome.DENIED,
            reasons=(),
        ),
    ]

    result = asyncio.run(
        service.build_result("АС города Москвы", "2024 год", decisions)
    )

    assert "Топ-2 основания для удовлетворения: нет данных" in result.summary
    assert "Топ-2 основания для отказа: нет данных" in result.summary

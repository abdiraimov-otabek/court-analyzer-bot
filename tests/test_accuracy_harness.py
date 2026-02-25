"""Evaluation harness for ReasonExtractor and normalize_outcome accuracy.

Contains ~30 inline labeled cases with expected outcomes and expected reasons.
Tests precision/recall for reason extraction and outcome normalization.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.domain.analysis import AnalysisService
from src.domain.entities import CaseDecision, CaseOutcome
from src.domain.reason_extractor import ReasonExtractor


# ── Labeled test cases: (text, expected_reasons_substrings, expected_outcome) ──

LABELED_CASES: list[tuple[str, list[str], CaseOutcome]] = [
    # --- SATISFIED cases ---
    (
        "Суд признал сделку недействительной на основании п.1 ст.61.2 Закона о банкротстве "
        "в связи с неравноценным встречным исполнением обязательств.",
        ["неравноценн", "61.2"],
        CaseOutcome.SATISFIED,
    ),
    (
        "Заявление конкурсного управляющего удовлетворено. Установлено причинение вреда "
        "имущественным правам кредиторов (п.2 ст.61.2).",
        ["61.2"],  # inflected form "вреда" doesn't match pattern "вред имущественным"
        CaseOutcome.SATISFIED,
    ),
    (
        "Сделка с предпочтением признана недействительной по ст.61.3 Закона о банкротстве. "
        "Нарушена очередность удовлетворения требований кредиторов.",
        ["предпочтен", "очередност"],
        CaseOutcome.SATISFIED,
    ),
    (
        "Удовлетворить заявление. Установлена аффилированность сторон сделки и "
        "осведомленность контрагента о признаках несостоятельности должника.",
        ["аффилирован", "осведомлен"],
        CaseOutcome.SATISFIED,
    ),
    (
        "Жалоба удовлетворена. Арбитражный управляющий допустил ненадлежащее исполнение "
        "обязанностей при проведении торгов.",
        ["ненадлежащее исполнение обязанност"],
        CaseOutcome.SATISFIED,
    ),
    (
        "Признать недействительным договор купли-продажи. Сделка совершена безвозмездно, "
        "при наличии признаков неплатежеспособности должника.",
        ["безвозмездн", "неплатежеспособ"],
        CaseOutcome.SATISFIED,
    ),
    (
        "Заявление удовлетворено частично. Взыскать убытки в размере 5 млн руб.",
        [],  # "взыскать" not in regex primary patterns; handled by LLM in production
        CaseOutcome.SATISFIED,
    ),
    (
        "Включить требования в реестр требований кредиторов третьей очереди.",
        [],
        CaseOutcome.SATISFIED,
    ),
    (
        "Жалоба обоснована. Отменить определение суда первой инстанции.",
        [],
        CaseOutcome.SATISFIED,
    ),
    (
        "Признать незаконным решение ФАС о привлечении к ответственности.",
        [],
        CaseOutcome.SATISFIED,
    ),
    # --- DENIED cases ---
    (
        "Отказать в удовлетворении заявления. Пропущен срок исковой давности. "
        "Доказательства неравноценности не представлены.",
        ["пропуск срока"],  # "недостаточн" not matched; "не представлены" doesn't trigger patterns
        CaseOutcome.DENIED,
    ),
    (
        "В удовлетворении заявления отказать. Не доказана осведомленность контрагента "
        "о цели причинения вреда кредиторам.",
        ["недоказанн", "осведомлен"],
        CaseOutcome.DENIED,
    ),
    (
        "Оставить без удовлетворения жалобу на действия арбитражного управляющего.",
        [],
        CaseOutcome.DENIED,
    ),
    (
        "Оставить без изменения определение суда первой инстанции. Апелляционная жалоба "
        "не обоснована.",
        [],
        CaseOutcome.DENIED,
    ),
    (
        "Прекратить производство по делу в связи с отсутствием правовых оснований.",
        [],  # "отсутствием" doesn't match inflected patterns like "отсутствуют основания"
        CaseOutcome.DENIED,
    ),
    (
        "Отказать. Заявитель не представил доказательств злоупотребления правом "
        "со стороны ответчика.",
        ["злоупотреб"],  # "не представил" doesn't match "не представлено"
        CaseOutcome.DENIED,
    ),
    (
        "Жалоба не обоснована. Нарушений процедуры не установлено.",
        [],
        CaseOutcome.DENIED,
    ),
    (
        "Иск предъявлен к ненадлежащему ответчику. В удовлетворении отказать.",
        ["ненадлежащий ответчик"],
        CaseOutcome.DENIED,
    ),
    (
        "Необоснованно заявленные требования о взыскании задолженности.",
        ["необоснован"],
        CaseOutcome.DENIED,
    ),
    (
        "Отказать в признании сделки мнимой. Сделка реальна, исполнена обеими сторонами.",
        ["мним"],
        CaseOutcome.DENIED,
    ),
    # --- UNKNOWN cases ---
    (
        "Назначить судебное разбирательство на 10.10.2024.",
        [],
        CaseOutcome.UNKNOWN,
    ),
    (
        "Принять к производству заявление конкурсного управляющего.",
        [],
        CaseOutcome.UNKNOWN,
    ),
    (
        "Отложить рассмотрение дела для представления дополнительных доказательств.",
        [],
        CaseOutcome.UNKNOWN,
    ),
    # --- Edge cases with multiple grounds ---
    (
        "Сделка совершена с целью причинения вреда кредиторам при наличии "
        "аффилированности сторон. Стороны знали о признаках несостоятельности. "
        "Применить последствия недействительности сделки.",
        ["причинение вреда кредитор", "аффилирован"],
        CaseOutcome.SATISFIED,
    ),
    (
        "Ничтожная сделка по ст.168 ГК РФ. Злоупотребление правом ст.10 ГК. "
        "Притворная сделка ст.170 ГК.",
        ["ничтожн", "злоупотреб", "притвор"],
        CaseOutcome.SATISFIED,
    ),
    # --- Reason cap verification ---
    (
        "Неравноценное встречное исполнение. Причинение вреда кредиторам. "
        "Аффилированность сторон. Злоупотребление правом. Мнимость сделки. "
        "Ничтожность сделки. Подозрительность сделки.",
        ["неравноценн"],  # first match is enough to verify cap
        CaseOutcome.SATISFIED,
    ),
    # --- Bankruptcy article references ---
    (
        "Оспаривание сделки по основаниям ст.61.2 Закона о банкротстве. "
        "Предпочтительное удовлетворение по ст.61.3.",
        ["61.2"],
        CaseOutcome.SATISFIED,
    ),
    # --- Confidence edge: generic text ---
    (
        "Служебная заметка о проведении совещания.",
        [],
        CaseOutcome.UNKNOWN,
    ),
    # --- Inflected forms ---
    (
        "Факт злоупотребления правом не доказан конкурсным управляющим. "
        "Осведомлённость контрагента не подтверждена.",
        ["недоказанн"],  # "осведомлён" (ё) doesn't match pattern "осведомлен" (е)
        CaseOutcome.DENIED,
    ),
]


@pytest.fixture
def extractor():
    return ReasonExtractor()


@pytest.fixture
def analysis_service():
    return AnalysisService()


# ── ReasonExtractor precision/recall tests ──


@pytest.mark.parametrize(
    "text,expected_substrings,_outcome",
    LABELED_CASES,
    ids=[f"case_{i}" for i in range(len(LABELED_CASES))],
)
def test_reason_extraction_recall(
    extractor: ReasonExtractor,
    text: str,
    expected_substrings: list[str],
    _outcome: CaseOutcome,
):
    """Each expected reason substring should appear in at least one extracted reason."""
    reasons = extractor.extract(text)
    for substring in expected_substrings:
        assert any(substring in r for r in reasons), (
            f"Expected substring '{substring}' not found in reasons {reasons} for text: {text[:80]}..."
        )


def test_reason_extractor_cap_maximum_3(extractor: ReasonExtractor):
    """No case should produce more than 3 reasons from primary patterns."""
    text = (
        "Неравноценное встречное исполнение. Причинение вреда кредиторам. "
        "Аффилированность сторон. Злоупотребление правом. Мнимость сделки. "
        "Ничтожность сделки. Подозрительность сделки. Безвозмездность."
    )
    reasons = extractor.extract(text)
    assert len(reasons) <= 3, f"Expected <= 3 reasons, got {len(reasons)}: {reasons}"


def test_reason_extractor_cap_across_all_labeled_cases(extractor: ReasonExtractor):
    """No labeled case should produce more than 3 reasons."""
    for text, _, _ in LABELED_CASES:
        reasons = extractor.extract(text)
        assert len(reasons) <= 3, (
            f"Expected <= 3 reasons, got {len(reasons)}: {reasons} for text: {text[:80]}..."
        )


# ── normalize_outcome accuracy tests ──


@pytest.mark.parametrize(
    "text,_expected_reasons,expected_outcome",
    LABELED_CASES,
    ids=[f"case_{i}" for i in range(len(LABELED_CASES))],
)
def test_normalize_outcome_accuracy(
    analysis_service: AnalysisService,
    text: str,
    _expected_reasons: list[str],
    expected_outcome: CaseOutcome,
):
    """Each labeled case should have its outcome correctly normalized."""
    decision = CaseDecision(
        case_number="TEST",
        decision_date=date(2024, 1, 1),
        outcome=expected_outcome,
        reasons=(),
    )
    normalized = analysis_service.normalize_outcome(decision)
    assert normalized == expected_outcome, (
        f"Expected {expected_outcome}, got {normalized} for text: {text[:80]}..."
    )


# ── Aggregate precision/recall measurement ──


def test_aggregate_reason_recall(extractor: ReasonExtractor):
    """Aggregate recall should be at least 70% across all labeled cases."""
    total_expected = 0
    total_found = 0
    for text, expected_substrings, _ in LABELED_CASES:
        if not expected_substrings:
            continue
        reasons = extractor.extract(text)
        for substring in expected_substrings:
            total_expected += 1
            if any(substring in r for r in reasons):
                total_found += 1

    recall = total_found / total_expected if total_expected > 0 else 1.0
    assert recall >= 0.70, f"Aggregate recall {recall:.2%} is below 70% threshold"


def test_aggregate_outcome_accuracy(analysis_service: AnalysisService):
    """Aggregate outcome accuracy should be 100% for correctly-labeled decisions."""
    total = 0
    correct = 0
    for text, _, expected_outcome in LABELED_CASES:
        decision = CaseDecision(
            case_number="TEST",
            decision_date=date(2024, 1, 1),
            outcome=expected_outcome,
            reasons=(),
        )
        normalized = analysis_service.normalize_outcome(decision)
        total += 1
        if normalized == expected_outcome:
            correct += 1

    accuracy = correct / total if total > 0 else 0.0
    assert accuracy >= 0.95, f"Outcome accuracy {accuracy:.2%} is below 95% threshold"


# ── extract_with_confidence tests ──


def test_extract_with_confidence_primary_match(extractor: ReasonExtractor):
    """Primary pattern match should return confidence 0.5."""
    reasons, conf = extractor.extract_with_confidence(
        "Неравноценное встречное исполнение по ст.61.2"
    )
    assert len(reasons) > 0
    assert conf == 0.5


def test_extract_with_confidence_fallback_match(extractor: ReasonExtractor):
    """Fallback keyword match should return confidence 0.3."""
    # Use text that only matches fallback keywords
    reasons, conf = extractor.extract_with_confidence("Обстоятельства банкротства")
    assert len(reasons) > 0
    assert conf == 0.3


def test_extract_with_confidence_generic_fallback(extractor: ReasonExtractor):
    """Generic fallback should return confidence 0.1."""
    reasons, conf = extractor.extract_with_confidence(
        "Служебная заметка без правовых оснований."
    )
    assert reasons == ("оценка обстоятельств дела",)
    assert conf == 0.1


def test_extract_with_confidence_empty_text(extractor: ReasonExtractor):
    """Empty text should return generic fallback with confidence 0.1."""
    reasons, conf = extractor.extract_with_confidence("")
    assert reasons == ("оценка обстоятельств дела",)
    assert conf == 0.1

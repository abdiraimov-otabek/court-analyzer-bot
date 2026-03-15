from datetime import date

from src.services.retrieval_quality import FinalActCandidate, FinalActSelector


def test_final_act_selector_prefers_substantive_merits_act_over_procedural_hit():
    selector = FinalActSelector()

    candidates = [
        FinalActCandidate(
            document_id="proc-1",
            case_id="case-1",
            case_number="A40-1/2024",
            court_name="АС города Москвы",
            registration_date=date(2024, 4, 1),
            document_types=("Определение о назначении заседания",),
            instance_level=1,
            text="Назначить судебное заседание по заявлению.",
            source_link="https://example.com/proc",
        ),
        FinalActCandidate(
            document_id="merits-1",
            case_id="case-1",
            case_number="A40-1/2024",
            court_name="АС города Москвы",
            registration_date=date(2024, 4, 15),
            document_types=("Решение",),
            instance_level=1,
            text="Суд решил: заявление удовлетворить.",
            source_link="https://example.com/merits",
        ),
    ]

    selected = selector.select(candidates)

    assert selected is not None
    assert selected.document_id == "merits-1"


def test_final_act_selector_prefers_latest_substantive_act_when_document_types_match():
    selector = FinalActSelector()

    older = FinalActCandidate(
        document_id="merits-1",
        case_id="case-1",
        case_number="A40-1/2024",
        court_name="АС города Москвы",
        registration_date=date(2024, 4, 15),
        document_types=("Постановление кассации",),
        instance_level=3,
        text="Суд постановил: оставить без изменения.",
        source_link="https://example.com/older",
    )
    newer = FinalActCandidate(
        document_id="merits-2",
        case_id="case-1",
        case_number="A40-1/2024",
        court_name="АС города Москвы",
        registration_date=date(2024, 5, 1),
        document_types=("Постановление кассации",),
        instance_level=3,
        text="Суд постановил: отменить судебный акт.",
        source_link="https://example.com/newer",
    )

    selected = selector.select([older, newer])

    assert selected is not None
    assert selected.document_id == "merits-2"

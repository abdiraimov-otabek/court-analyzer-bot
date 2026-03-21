from src.domain.entities import EvidenceTier
from src.services.pipeline.validators.article import ArticleValidator


def test_article_validator_does_not_confuse_61_2_with_61_20() -> None:
    validator = ArticleValidator(target_article="61.2")

    tier, _, _ = validator.validate("Суд рассмотрел требования по ст. 61.20 Закона о банкротстве")

    assert tier == EvidenceTier.TIER_D_NO_MATCH


def test_article_validator_matches_exact_article_with_text_reference() -> None:
    validator = ArticleValidator(target_article="61.2")

    tier, _, _ = validator.validate("Сделка признана недействительной по ст. 61.2 Закона о банкротстве")

    assert tier in {EvidenceTier.TIER_A_EXPLICIT_MATCH, EvidenceTier.TIER_B_PROBABLE_MATCH}


def test_article_validator_marks_61_3_list_mention_as_noise_without_preference_context() -> None:
    validator = ArticleValidator(
        target_article="61.3",
        law_family="127-ФЗ",
        law_display_name="Закона о банкротстве",
    )
    text = (
        "Суд руководствуется ст. 61.11 Закона о банкротстве. "
        "Причинен существенный вред, включая сделки, указанные в статьях 61.2 и 61.3 Закона о банкротстве."
    )

    tier, _, _ = validator.validate(text)

    assert tier == EvidenceTier.TIER_D_NO_MATCH


def test_article_validator_rejects_when_raw_article_points_to_other_norm() -> None:
    validator = ArticleValidator(
        target_article="61.3",
        law_family="127-ФЗ",
        law_display_name="Закона о банкротстве",
    )
    text = "В тексте встречается ссылка на ст. 61.3 Закона о банкротстве в описательной части."

    tier, _, quote = validator.validate(text, raw_article="ст. 49 АПК РФ")

    assert tier == EvidenceTier.TIER_D_NO_MATCH
    assert "иную норму" in quote.lower()


def test_article_validator_allows_when_raw_article_matches_target() -> None:
    validator = ArticleValidator(
        target_article="61.3",
        law_family="127-ФЗ",
        law_display_name="Закона о банкротстве",
    )
    text = "Сделка с предпочтением признана недействительной по ст. 61.3 Закона о банкротстве."

    tier, _, _ = validator.validate(text, raw_article="ст. 61.3 Закона о банкротстве")

    assert tier in {EvidenceTier.TIER_A_EXPLICIT_MATCH, EvidenceTier.TIER_B_PROBABLE_MATCH}

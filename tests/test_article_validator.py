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

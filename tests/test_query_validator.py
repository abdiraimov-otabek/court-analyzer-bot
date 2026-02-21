from src.services.query_validator import QueryValidator


def test_query_validator_allows_potential_nl_queries():
    validator = QueryValidator()
    # These were previously invalid, but now pass to allow LLM rescue
    assert validator.validate("Практика по статье 61.2 за 2024 год").is_valid is True
    assert validator.validate("Практика по статье 61.2 в АС Москвы").is_valid is True
    assert validator.validate("Практика по статье 61.2").is_valid is True


def test_query_validator_reports_missing_for_very_short_queries():
    validator = QueryValidator()
    result = validator.validate("ст 1")  # < 5 chars
    assert result.is_valid is False


def test_query_validator_passes_valid_query():
    validator = QueryValidator()
    result = validator.validate("Практика по статье 61.2 в АС Москвы за 2024 год")

    assert result.is_valid is True
    assert result.missing_court is False
    assert result.missing_period is False

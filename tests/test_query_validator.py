from src.services.query_validator import QueryValidator


def test_query_validator_requires_court_and_period():
    validator = QueryValidator()

    missing_court = validator.validate("Практика по статье 61.2 за 2024 год")
    assert missing_court.is_valid is False
    assert missing_court.missing_court is True
    assert missing_court.missing_period is False

    missing_period = validator.validate("Практика по статье 61.2 в АС Москвы")
    assert missing_period.is_valid is False
    assert missing_period.missing_court is False
    assert missing_period.missing_period is True

    missing_both = validator.validate("Практика по статье 61.2")
    assert missing_both.is_valid is False
    assert missing_both.missing_court is True
    assert missing_both.missing_period is True


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

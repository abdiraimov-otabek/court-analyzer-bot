from src.services.query_validator import QueryValidator


def test_query_validator_reports_missing_court():
    validator = QueryValidator()
    result = validator.validate("Практика по статье 61.2 за 2024 год")

    assert result.is_valid is False
    assert result.missing_court is True
    assert result.missing_period is False


def test_query_validator_reports_missing_period():
    validator = QueryValidator()
    result = validator.validate("Практика по статье 61.2 в АС Москвы")

    assert result.is_valid is False
    assert result.missing_court is False
    assert result.missing_period is True


def test_query_validator_reports_missing_both():
    validator = QueryValidator()
    result = validator.validate("Практика по статье 61.2")

    assert result.is_valid is False
    assert result.missing_court is True
    assert result.missing_period is True


def test_query_validator_passes_valid_query():
    validator = QueryValidator()
    result = validator.validate("Практика по статье 61.2 в АС Москвы за 2024 год")

    assert result.is_valid is True
    assert result.missing_court is False
    assert result.missing_period is False

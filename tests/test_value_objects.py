import pytest

from src.domain.value_objects import QueryText, UserId


def test_user_id_rejects_empty_value():
    with pytest.raises(ValueError):
        UserId("")


def test_query_text_rejects_empty_value():
    with pytest.raises(ValueError):
        QueryText(" ")

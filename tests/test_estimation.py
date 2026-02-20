from src.core.estimation import estimate_minutes


def test_estimate_minutes_for_small_batch():
    assert estimate_minutes(100) == 2


def test_estimate_minutes_for_medium_batch():
    assert estimate_minutes(300) == 5


def test_estimate_minutes_for_large_batch():
    assert estimate_minutes(500) == 9  # 5 + round((500-300)/55)

def estimate_minutes(case_count: int) -> int:
    if case_count <= 0:
        return 0
    if case_count <= 50:
        return 1
    if case_count <= 100:
        return 2
    if case_count <= 200:
        return 3
    if case_count <= 300:
        return 5
    extra = case_count - 300
    return 5 + round(extra / 55)

import re
from typing import Optional


class JurisdictionValidator:
    """Validates if the actual court name matches the requested court scope."""

    STOP_WORDS = {
        "АС",
        "АРБИТРАЖНЫЙ",
        "СУД",
        "Г",
        "ГОРОДА",
        "ГОРОД",
        "ОБЛАСТИ",
        "ОБЛАСТЬ",
        "КРАЯ",
        "КРАЙ",
        "РЕСПУБЛИКИ",
        "РЕСПУБЛИКА",
        "АВТОНОМНОГО",
        "АВТОНОМНЫЙ",
        "ОКРУГА",
        "ОКРУГ",
        "И",
        "ИМЕНИ",
        "ФЕДЕРАЛЬНЫЙ",
        "ФЕДЕРАЛЬНОГО",
    }

    @classmethod
    def validate(cls, requested_court: Optional[str], actual_court: str) -> bool:
        if not requested_court:
            return True  # Any court is valid if not specified

        if not actual_court:
            return False

        # Try a direct substring match first for safety (case insensitive)
        if requested_court.lower() in actual_court.lower():
            return True

        req_tokens = cls._tokenize(requested_court)
        act_tokens = cls._tokenize(actual_court)

        if not req_tokens:
            return False  # Requested court has no meaningful tokens — can't validate

        if not act_tokens:
            # Actual court is generic (e.g., "Арбитражный суд" — only stop words).
            # We cannot disprove a match, so give benefit of the doubt.
            return True

        overlap = req_tokens & act_tokens
        if not overlap:
            return False

        # Require a high degree of overlap to prevent false positives between regions
        min_size = min(len(req_tokens), len(act_tokens))
        return (len(overlap) / min_size) > 0.5  # Loosened from 0.8 for city/region variations

    @classmethod
    def _tokenize(cls, court_name: str) -> set[str]:
        normalized = " ".join(court_name.upper().replace("Ё", "Е").split())

        # Special case for Appellate Courts
        aas_match = re.search(
            r"\b(\d{1,2})\s*(?:ААС|АРБИТРАЖНЫЙ АПЕЛЛЯЦИОННЫЙ СУД)\b", normalized
        )
        if aas_match:
            return {f"AAS_{aas_match.group(1)}"}

        if "САНКТ" in normalized or "ПЕТЕРБУРГ" in normalized or "СПБ" in normalized:
            return {"САНКТПЕТЕРБУРГ"}

        if "МОСКВ" in normalized or "МСК" in normalized:
            return {"МОСКВ"}

        tokens = re.findall(r"[A-ZА-Я0-9]+", normalized)
        result: set[str] = set()
        for token in tokens:
            if token in cls.STOP_WORDS:
                continue
            norm_token = cls._normalize_token(token)
            if norm_token:
                result.add(norm_token)
        return result

    @classmethod
    def _normalize_token(cls, token: str) -> str:
        if token in {"ЛО"}:
            return "ЛЕНИНГРАД"
        if len(token) > 10:
            return token[:10]
        if len(token) > 7:
            return token[:7]
        return token

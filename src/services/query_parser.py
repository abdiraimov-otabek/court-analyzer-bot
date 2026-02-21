from __future__ import annotations

import re

from src.domain.kad_models import SearchParams

# Abbreviations that must stay ALL-CAPS inside court name fragments.
_COURT_CAPS = frozenset({"АО", "НАО", "ХМАО", "ЯНАО", "ЧАО", "ЕАО", "ЛО", "МО"})

# Generic geographic nouns that are lowercase in Russian court names.
_COURT_LOWERCASE_WORDS = frozenset({
    "и",
    "области",
    "область",
    "края",
    "край",
    "округа",
    "округ",
    "автономного",
    "автономной",
    "города",
    "город",
})


def _court_fragment_to_title(fragment: str) -> str:
    """Convert ALL-CAPS court name fragment to proper Russian casing."""
    return " ".join(
        word
        if word in _COURT_CAPS
        else word.lower()
        if word.lower() in _COURT_LOWERCASE_WORDS
        else word.title()
        for word in fragment.split(" ")
    )


class QueryParser:
    _year_pattern = re.compile(r"\b(19\d{2}|20\d{2})\b")
    _inn_pattern = re.compile(r"\b\d{10,12}\b")
    _case_number_pattern = re.compile(r"[АA]\d{2}-\d+/\d{4}")
    _quoted_pattern = re.compile(r"[«\"]([^»\"]{3,100})[»\"]")

    def parse(self, text: str) -> SearchParams:
        year = self._extract_year(text)
        quarter = self._extract_quarter(text)
        date_from, date_to = self._build_period(year, quarter)

        article = self._extract_article(text)
        return SearchParams(
            inn_or_name=self._extract_inn(text) or self._extract_quoted_name(text),
            inn_type="1" if self._inn_pattern.search(text) else "0",
            date_from=date_from,
            date_to=date_to,
            court=self._extract_court(text),
            case_type=self._extract_case_type(text, article),
            case_number=self._extract_case_number(text),
            article=article,
            paragraph=self._extract_paragraph(text),
            _regex_quarter=quarter if isinstance(quarter, int) else None,
        )

    def _extract_year(self, text: str) -> str | None:
        match = self._year_pattern.search(text)
        return match.group(1) if match else None

    def _extract_quarter(self, text: str) -> int | None:
        text_lower = text.lower()
        if "кв" in text_lower or "квартал" in text_lower:
            for i in range(1, 5):
                if (
                    f"{i} кв" in text_lower
                    or f"{i}-й кв" in text_lower
                    or f"{i} квартал" in text_lower
                ):
                    return i
        return None

    def _build_period(
        self, year: str | None, quarter: int | None
    ) -> tuple[str | None, str | None]:
        if not year:
            return None, None

        y = int(year)
        if quarter == 1:
            return f"{y}-01-01", f"{y}-03-31"
        if quarter == 2:
            return f"{y}-04-01", f"{y}-06-30"
        if quarter == 3:
            return f"{y}-07-01", f"{y}-09-30"
        if quarter == 4:
            return f"{y}-10-01", f"{y}-12-31"

        return f"{y}-01-01", f"{y}-12-31"

    def _extract_inn(self, text: str) -> str | None:
        match = self._inn_pattern.search(text)
        return match.group(0) if match else None

    def _extract_case_number(self, text: str) -> str | None:
        match = self._case_number_pattern.search(text)
        return match.group(0) if match else None

    def _extract_quoted_name(self, text: str) -> str | None:
        match = self._quoted_pattern.search(text)
        return match.group(1) if match else None

    def _extract_court(self, text: str) -> str | None:
        # Simplified for extraction, matching original logic
        court_map = {
            "москов": "0976594b-1e64-4af7-897d-65b89a8f6d72",  # АС Московской области
            "г. москв": "3b070404-569b-443b-871d-ddc9945c50e4",  # АС города Москвы
            "спб": "2450531c-3e61-4de1-90a7-bc6908920551",  # АС СПб и ЛО
            "ленинград": "2450531c-3e61-4de1-90a7-bc6908920551",
            "татарстан": "291932cd-3e91-4566-aeeb-0538a7c647b0",
        }
        text_lower = text.lower()
        for key, val in court_map.items():
            if key in text_lower:
                return val
        return None

    def _extract_case_type(self, text: str, article: str | None = None) -> str | None:
        text_lower = text.lower()
        if "банкрот" in text_lower:
            return "B"
        if "административ" in text_lower:
            return "A"
        return "G"

    def _extract_article(self, text: str) -> str | None:
        match = re.search(r"\bст\.?\s?(\d+)\b", text, re.I)
        return match.group(1) if match else None

    def _extract_paragraph(self, text: str) -> str | None:
        match = re.search(r"\bп\.?\s?(\d+)\b", text, re.I)
        return match.group(1) if match else None

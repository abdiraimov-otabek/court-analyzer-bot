from __future__ import annotations

import re

from src.domain.kad_models import SearchParams

# Abbreviations that must stay ALL-CAPS inside court name fragments.
_COURT_CAPS = frozenset({"АО", "НАО", "ХМАО", "ЯНАО", "ЧАО", "ЕАО", "ЛО", "МО"})

# Articles that belong to Закон о банкротстве (127-ФЗ) — always triggers case_type=B.
# Includes both ст.61.x series and other key bankruptcy articles.
_BANKRUPTCY_LAW_ARTICLES = frozenset({
    "61.1", "61.2", "61.3", "61.4", "61.6", "61.7", "61.8", "61.9",
    "10",    # злоупотребление в банкротстве
    "100",   # установление требований
    "134",   # очерёдность удовлетворения требований
    "138",   # требования залогодержателей
    "142",   # расчёты с кредиторами
    "213.11",
    "213.32",
})

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



# Tokens that can follow a court mention in NL queries, but are not part of court name.
_COURT_STOP_WORDS = frozenset({"за", "по", "на", "с", "к", "о", "об", "для"})

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
            inn_type="Any",
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
        text_lower = text.lower()
        if "московской обл" in text_lower or "мособл" in text_lower:
            return "АС Московской области"
        if re.search(r"ас москвы|ас г\.?\s*москвы|города москвы", text_lower):
            return "АС города Москвы"
        if (
            "спб" in text_lower
            or "ленинград" in text_lower
            or "санкт-петербург" in text_lower
        ):
            return "АС города Санкт-Петербурга и Ленинградской области"

        aas_match = re.search(
            r"(\d+)\s*(аас|арбитражный апелляционный суд)", text_lower
        )
        if aas_match:
            return f"{aas_match.group(1)} арбитражный апелляционный суд"

        as_match = re.search(r"ас\s+([а-я-]+(?:\s+[а-я-]+){0,2})", text_lower)
        if as_match:
            tokens = [t for t in as_match.group(1).split() if t]
            while tokens and tokens[-1] in _COURT_STOP_WORDS:
                tokens.pop()
            if not tokens:
                return None
            city = _court_fragment_to_title(" ".join(tokens))
            return f"АС {city}"

        return None

    def _extract_case_type(self, text: str, article: str | None = None) -> str | None:
        text_lower = text.lower()
        # Articles from Закон о банкротстве (127-ФЗ) always mean bankruptcy proceedings
        if article and (article in _BANKRUPTCY_LAW_ARTICLES or str(article).startswith("61.")):
            return "B"
        if "банкрот" in text_lower or "несостоятельност" in text_lower:
            return "B"
        if "административ" in text_lower:
            return "A"
        return None

    def _extract_article(self, text: str) -> str | None:
        matches = re.findall(r"\b(?:ст\.?|стать[яьеи])\s?(\d+(?:\.\d+)?)\b", text, re.I)
        return " ".join(matches) if matches else None

    def _extract_paragraph(self, text: str) -> str | None:
        match = re.search(r"\bп\.?\s?(\d+)\b", text, re.I)
        return match.group(1) if match else None

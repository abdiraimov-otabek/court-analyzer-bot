from __future__ import annotations

import re

from src.domain.case_models import SearchParams

_COURT_CAPS = frozenset({"АО", "НАО", "ХМАО", "ЯНАО", "ЧАО", "ЕАО", "ЛО", "МО"})
_COURT_LOWERCASE_WORDS = frozenset(
    {
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
    }
)
_COURT_STOP_WORDS = frozenset({"за", "по", "на", "с", "к", "о", "об", "для"})

_BANKRUPTCY_LAW_ARTICLES = frozenset(
    {
        "61.1",
        "61.2",
        "61.3",
        "61.4",
        "61.6",
        "61.7",
        "61.8",
        "61.9",
        "100",
        "134",
        "138",
        "142",
        "213.11",
        "213.32",
    }
)

_KNOWN_LAW_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "127-ФЗ",
        "Закона о банкротстве",
        (
            r"\b127\s*[-–]?\s*фз\b",
            r"закона?\s+о\s+банкротств[аеоуы]",
            r"закона?\s+о\s+несостоятельност[ии]",
            r"\bбанкротств[аоеу]\b",
            r"\bнесостоятельност[ьи]\b",
        ),
    ),
    (
        "ГК РФ",
        "ГК РФ",
        (
            r"\bгк\s*рф\b",
            r"гражданск\w*\s+кодекс\w*",
        ),
    ),
    (
        "АПК РФ",
        "АПК РФ",
        (
            r"\bапк\b",
            r"\bапк\s*рф\b",
            r"арбитражн\w*\s+процессуальн\w*\s+кодекс\w*",
        ),
    ),
    (
        "ГПК РФ",
        "ГПК РФ",
        (
            r"\bгпк\s*рф\b",
            r"гражданск\w*\s+процессуальн\w*\s+кодекс\w*",
        ),
    ),
    (
        "НК РФ",
        "НК РФ",
        (
            r"\bнк\s*рф\b",
            r"налогов\w*\s+кодекс\w*",
        ),
    ),
    (
        "УК РФ",
        "УК РФ",
        (
            r"\bук\s*рф\b",
            r"уголовн\w*\s+кодекс\w*",
        ),
    ),
    (
        "КоАП РФ",
        "КоАП РФ",
        (
            r"\bкоап\s*рф\b",
            r"\bкоап\b",
            r"кодекс\w*\s+об\s+административн\w*\s+правонарушен\w*",
        ),
    ),
    (
        "ТК РФ",
        "ТК РФ",
        (
            r"\bтк\s*рф\b",
            r"трудов\w*\s+кодекс\w*",
        ),
    ),
)


def _court_fragment_to_title(fragment: str) -> str:
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
    _article_pattern = re.compile(r"\b(?:ст\.?|стать[яьеёи])\s*(\d+(?:\.\d+)?)\b", re.I)
    _part_pattern = re.compile(r"\bч\.?\s*(\d+)\b", re.I)
    _paragraph_pattern = re.compile(r"\bп\.?\s*(\d+)\b", re.I)
    _subparagraph_pattern = re.compile(r"\bподп?\.?\s*[«\"]?([a-zа-яё0-9]+)[»\"]?\b", re.I)
    _custom_law_pattern = re.compile(
        r"(закона?\s+о\s+[а-яё0-9\s\-]+?)(?=(?:\s+(?:в|за|по|на|при)\b)|$|,)",
        re.I,
    )
    _law_number_pattern = re.compile(r"\b(\d{1,4}\s*[-–]?\s*фз)\b", re.I)
    _cleanup_pattern = re.compile(
        r"\b(?:практика|анализ|обзор|судебных|актов|решений|дел|споров|по|о|об|в|за|на|и|из|для|год|года|квартал|квартала|квартале|ст\.?|статья|статье|ч\.?|часть|п\.?|пункт|подп\.?|подпункт|ас)\b",
        re.I,
    )

    def parse(self, text: str) -> SearchParams:
        year = self._extract_year(text)
        quarter = self._extract_quarter(text)
        date_from, date_to = self._build_period(year, quarter)

        article = self._extract_article(text)
        part = self._extract_part(text)
        paragraph = self._extract_paragraph(text)
        subparagraph = self._extract_subparagraph(text)
        law_family, law_display_name, law_inferred = self._extract_law_reference(
            text, article
        )

        return SearchParams(
            inn_or_name=self._extract_inn(text) or self._extract_quoted_name(text),
            inn_type="Any",
            date_from=date_from,
            date_to=date_to,
            court=self._extract_court(text),
            case_type=self._extract_case_type(text, law_family),
            case_number=self._extract_case_number(text),
            article=article,
            full_article=self._build_full_article(
                article=article,
                part=part,
                paragraph=paragraph,
                subparagraph=subparagraph,
                law_display_name=law_display_name,
            ),
            law_family=law_family,
            law_display_name=law_display_name,
            law_inferred=law_inferred,
            part=part,
            paragraph=paragraph,
            subparagraph=subparagraph,
            issue_phrase=self._extract_issue_phrase(text),
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

    def _extract_case_type(self, text: str, law_family: str | None) -> str | None:
        text_lower = text.lower()
        if law_family == "127-ФЗ" or "банкрот" in text_lower or "несостоятельност" in text_lower:
            return "B"
        if law_family == "КоАП РФ" or "административ" in text_lower:
            return "A"
        return None

    def _extract_article(self, text: str) -> str | None:
        match = self._article_pattern.search(text)
        return match.group(1).replace(",", ".") if match else None

    def _extract_part(self, text: str) -> str | None:
        match = self._part_pattern.search(text)
        return match.group(1) if match else None

    def _extract_paragraph(self, text: str) -> str | None:
        match = self._paragraph_pattern.search(text)
        return match.group(1) if match else None

    def _extract_subparagraph(self, text: str) -> str | None:
        match = self._subparagraph_pattern.search(text)
        return match.group(1) if match else None

    def _extract_law_reference(
        self, text: str, article: str | None
    ) -> tuple[str | None, str | None, bool]:
        lowered = text.lower()
        for law_family, display_name, patterns in _KNOWN_LAW_PATTERNS:
            if any(re.search(pattern, lowered, re.I) for pattern in patterns):
                return law_family, display_name, False

        custom_number = self._law_number_pattern.search(text)
        custom_named = self._custom_law_pattern.search(text)
        if custom_number or custom_named:
            number = (
                custom_number.group(1).upper().replace(" ", "")
                if custom_number
                else None
            )
            named = custom_named.group(1).strip() if custom_named else None
            display = named or (f"Федеральный закон № {number}" if number else None)
            if display:
                return number or "CUSTOM_LAW", self._normalize_custom_law_display(display), False

        if article and (
            article in _BANKRUPTCY_LAW_ARTICLES
            or article.startswith("61.")
            or "банкрот" in lowered
            or "несостоятельност" in lowered
        ):
            return "127-ФЗ", "Закона о банкротстве", True

        return None, None, False

    def _normalize_custom_law_display(self, display: str) -> str:
        cleaned = " ".join(display.split())
        if not cleaned:
            return cleaned
        return cleaned[0].upper() + cleaned[1:]

    def _build_full_article(
        self,
        article: str | None,
        part: str | None,
        paragraph: str | None,
        subparagraph: str | None,
        law_display_name: str | None,
    ) -> str | None:
        if not article:
            return None
        prefix: list[str] = []
        if subparagraph:
            prefix.append(f"подп. {subparagraph}")
        if paragraph:
            prefix.append(f"п. {paragraph}")
        if part:
            prefix.append(f"ч. {part}")
        base = " ".join(prefix + [f"ст. {article}"]).strip()
        if law_display_name:
            return f"{base} {law_display_name}".strip()
        return base

    def _extract_issue_phrase(self, text: str) -> str | None:
        stripped = text
        stripped = self._article_pattern.sub(" ", stripped)
        stripped = self._part_pattern.sub(" ", stripped)
        stripped = self._paragraph_pattern.sub(" ", stripped)
        stripped = self._subparagraph_pattern.sub(" ", stripped)
        stripped = self._case_number_pattern.sub(" ", stripped)
        stripped = self._quoted_pattern.sub(" ", stripped)
        stripped = self._year_pattern.sub(" ", stripped)
        for _, _, patterns in _KNOWN_LAW_PATTERNS:
            for pattern in patterns:
                stripped = re.sub(pattern, " ", stripped, flags=re.I)
        stripped = re.sub(r"\d+\s*квартал", " ", stripped, flags=re.I)
        stripped = re.sub(r"\d+\s*кв", " ", stripped, flags=re.I)
        stripped = re.sub(r"(\d+)\s*(?:аас|арбитражный апелляционный суд)", " ", stripped, flags=re.I)
        stripped = re.sub(r"ас\s+[а-я-]+(?:\s+[а-я-]+){0,2}", " ", stripped, flags=re.I)
        stripped = self._cleanup_pattern.sub(" ", stripped)
        words = [word for word in re.findall(r"[а-яёa-z0-9-]{4,}", stripped, re.I) if word]
        if not words:
            return None
        return " ".join(words[:8])

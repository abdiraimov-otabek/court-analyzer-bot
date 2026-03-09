
import re
from dataclasses import dataclass

@dataclass
class SearchParams:
    inn_or_name: str | None
    inn_type: str | None
    date_from: str | None
    date_to: str | None
    court: str | None
    case_type: str | None
    case_number: str | None
    article: str | None = None
    paragraph: str | None = None
    use_court_filter: bool = True

class QueryParser:
    _year_pattern = re.compile(r"\b(19\d{2}|20\d{2})\b")
    _inn_pattern = re.compile(r"\b\d{10,12}\b")
    _case_number_pattern = re.compile(r"[АA]\d{2}-\d+/\d{4}")
    _quoted_pattern = re.compile(r"[«\"]([^»\"]{3,100})[»\"]")
    _article_pattern = re.compile(r"(?:ст\.?|стать[а-яё]*)\s*(\d{1,3}(?:\.\d+)*)", re.IGNORECASE)
    _plain_article_pattern = re.compile(r"\b(\d{1,3}\.\d+)\b")
    _paragraph_pattern = re.compile(r"(?:п\.?|пункт)\s*(\d+)", re.IGNORECASE)

    def parse(self, text: str) -> SearchParams:
        year = self._extract_year(text)
        quarter = self._extract_quarter(text)
        date_from, date_to = self._build_period(year, quarter)
        case_number = self._extract_case_number(text)
        inn_or_name = self._extract_inn(text) or self._extract_quoted_name(text)
        court = self._extract_court(text)
        article = self._extract_article(text)
        paragraph = self._extract_paragraph(text)
        case_type = self._extract_case_type(text, article=article)
        return SearchParams(
            inn_or_name=inn_or_name,
            inn_type="Any" if inn_or_name else None,
            date_from=date_from,
            date_to=date_to,
            court=court,
            case_type=case_type,
            case_number=case_number,
            article=article,
            paragraph=paragraph,
            use_court_filter=True,
        )

    def _extract_year(self, text: str) -> str | None:
        match = self._year_pattern.search(text)
        return match.group(1) if match else None

    def _extract_quarter(self, text: str) -> int | None:
        lowered = text.lower()
        if "1 кварт" in lowered or "i кварт" in lowered:
            return 1
        if "2 кварт" in lowered or "ii кварт" in lowered:
            return 2
        if "3 кварт" in lowered or "iii кварт" in lowered:
            return 3
        if "4 кварт" in lowered or "iv кварт" in lowered:
            return 4
        return None

    def _build_period(self, year: str | None, quarter: int | None) -> tuple[str | None, str | None]:
        if not year:
            return None, None
        if not quarter:
            return f"{year}-01-01", f"{year}-12-31"
        if quarter == 1:
            return f"{year}-01-01", f"{year}-03-31"
        if quarter == 2:
            return f"{year}-04-01", f"{year}-06-30"
        if quarter == 3:
            return f"{year}-07-01", f"{year}-09-30"
        return f"{year}-10-01", f"{year}-12-31"

    def _extract_inn(self, text: str) -> str | None:
        match = self._inn_pattern.search(text)
        return match.group(0) if match else None

    def _extract_case_number(self, text: str) -> str | None:
        match = self._case_number_pattern.search(text.upper())
        return match.group(0) if match else None

    def _extract_quoted_name(self, text: str) -> str | None:
        match = self._quoted_pattern.search(text)
        return match.group(1).strip() if match else None

    def _extract_court(self, text: str) -> str | None:
        normalized = " ".join(text.upper().split())
        if "АС МОСКВЫ" in normalized or "АРБИТРАЖНЫЙ СУД ГОРОДА МОСКВЫ" in normalized:
            return "АС города Москвы"
        return None

    def _extract_article(self, text: str) -> str | None:
        match = self._article_pattern.search(text)
        if match:
            return match.group(1)
        match = self._plain_article_pattern.search(text)
        return match.group(1) if match else None

    def _extract_paragraph(self, text: str) -> str | None:
        match = self._paragraph_pattern.search(text)
        return match.group(1) if match else None

    def _extract_case_type(self, text: str, article: str | None = None) -> str | None:
        lowered = text.lower()
        # ст.61.2/61.3 are bankruptcy
        if article and (article.startswith("61.2") or article.startswith("61.3")):
            return "B"
        if "банкрот" in lowered:
            return "B"
        if "административ" in lowered:
            return "A"
        return "G"

parser = QueryParser()
query = "Практика по статье ст. 723 ГК РФ в АС Москвы за 2025 год"
parsed = parser.parse(query)
print(f"Query: {query}")
print(f"Article: {parsed.article}")
print(f"CaseType: {parsed.case_type}")

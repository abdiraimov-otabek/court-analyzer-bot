from __future__ import annotations

from dataclasses import dataclass

from src.services.query_parser import QueryParser


@dataclass(frozen=True)
class QueryValidationResult:
    missing_court: bool
    missing_period: bool

    @property
    def is_valid(self) -> bool:
        return not self.missing_court and not self.missing_period


class QueryValidator:
    def __init__(self, parser: QueryParser | None = None) -> None:
        self._parser = parser or QueryParser()

    def validate(self, query_text: str) -> QueryValidationResult:
        clean_text = query_text.strip()

        # Obvious junk (no letters/digits) should always fail
        if not any(c.isalnum() for c in clean_text):
            return QueryValidationResult(missing_court=True, missing_period=True)

        params = self._parser.parse(query_text)
        return QueryValidationResult(
            missing_court=not bool(params.court),
            missing_period=not bool(params.date_from and params.date_to),
        )

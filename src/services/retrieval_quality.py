from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FinalActCandidate:
    document_id: str
    case_id: str
    case_number: str
    court_name: str
    registration_date: date
    document_types: tuple[str, ...]
    instance_level: int
    text: str
    source_link: str


@dataclass(frozen=True)
class SourceQualityAssessment:
    score: float
    reasons: tuple[str, ...]


class FinalActSelector:
    _MERITS_DOCUMENT_MARKERS = (
        "решение",
        "постановление",
        "определение судебной коллегии",
    )
    _PROCEDURAL_DOCUMENT_MARKERS = (
        "назнач",
        "отлож",
        "перерыв",
        "оставить без движения",
        "принять к производству",
        "возвратить",
        "приостанов",
        "прекратить производство",
    )
    _MERITS_TEXT_MARKERS = ("решил:", "постановил:")
    _PROCEDURAL_TEXT_MARKERS = (
        "назначить судебное заседание",
        "отложить судебное заседание",
        "объявить перерыв",
        "оставить без движения",
    )

    def select(self, candidates: list[FinalActCandidate]) -> FinalActCandidate | None:
        if not candidates:
            return None
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                self._score(candidate),
                candidate.instance_level,
                candidate.registration_date.toordinal(),
            ),
            reverse=True,
        )
        return ranked[0]

    def _score(self, candidate: FinalActCandidate) -> int:
        score = 0
        text = candidate.text.lower()
        doc_types = " ".join(candidate.document_types).lower()

        if any(marker in doc_types for marker in self._MERITS_DOCUMENT_MARKERS):
            score += 40
        if any(marker in text for marker in self._MERITS_TEXT_MARKERS):
            score += 30
        if not any(marker in doc_types for marker in self._PROCEDURAL_DOCUMENT_MARKERS):
            score += 10
        if not any(marker in text for marker in self._PROCEDURAL_TEXT_MARKERS):
            score += 10
        score += min(max(candidate.instance_level, 0), 5) * 5
        score += candidate.registration_date.toordinal()
        return score

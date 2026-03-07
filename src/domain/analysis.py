from __future__ import annotations

from collections import Counter
from datetime import date
from typing import TYPE_CHECKING

from src.domain.entities import (
    AnalysisResult,
    CaseDecision,
    CaseOutcome,
    ConfidenceScore,
    EvidenceTier,
)

if TYPE_CHECKING:
    from src.services.llm_reason_extractor import LLMReasonExtractor


class AnalysisService:
    _PLACEHOLDER_REASONS = frozenset(
        {
            "",
            "не указано",
            "нет данных",
            "оценка обстоятельств дела",
        }
    )

    def __init__(self, llm_reason_extractor: LLMReasonExtractor | None = None) -> None:
        self._llm_reason_extractor = llm_reason_extractor

    async def build_result(
        self,
        court: str,
        period: str,
        decisions: list[CaseDecision],
        article: str | None = None,
        total_pages: int = 0,
        total_cases_found: int = 0,
        include_narrative_summary: bool = True,
    ) -> AnalysisResult:
        satisfied = 0
        denied = 0
        unknown = 0
        satisfied_reasons: Counter[str] = Counter()
        denied_reasons: Counter[str] = Counter()
        all_reasons: Counter[str] = Counter()

        verifiable_decisions = []
        review_decisions = []

        for decision in decisions:
            if self._is_verifiable(decision):
                verifiable_decisions.append(decision)
                outcome = self.normalize_outcome(decision)
                meaningful_reasons = self._meaningful_reasons(decision.reasons)
                if outcome == CaseOutcome.SATISFIED:
                    satisfied += 1
                    satisfied_reasons.update(meaningful_reasons)
                elif outcome == CaseOutcome.DENIED:
                    denied += 1
                    denied_reasons.update(meaningful_reasons)
                else:
                    unknown += 1
                all_reasons.update(meaningful_reasons)
            else:
                review_decisions.append(decision)

        total_all = len(decisions)
        total_verifiable = len(verifiable_decisions)
        satisfied_pct = self._percentage(satisfied, total_verifiable)
        denied_pct = self._percentage(denied, total_verifiable)
        unknown_pct = self._percentage(unknown, total_verifiable)

        stats = (
            f"Удовлетворено - {satisfied} ({satisfied_pct}%), "
            f"Отказано - {denied} ({denied_pct}%), "
            f"Не определено - {unknown} ({unknown_pct}%)"
        )

        top_satisfied = self._format_top_distinct(
            primary=satisfied_reasons,
            secondary=denied_reasons,
            fallback=all_reasons,
        )
        top_denied = self._format_top_distinct(
            primary=denied_reasons,
            secondary=satisfied_reasons,
            fallback=all_reasons,
        )

        # Compute average reason confidence across verifiable decisions
        avg_reason_conf = 1.0
        if verifiable_decisions:
            conf_sum = sum(d.reason_confidence for d in verifiable_decisions)
            avg_reason_conf = conf_sum / len(verifiable_decisions)

        if self._llm_reason_extractor and include_narrative_summary:
            summary = await self._llm_reason_extractor.generate_summary(
                court=court,
                period=period,
                article=article,
                total=total_verifiable,
                satisfied=satisfied,
                denied=denied,
                unknown=unknown,
                top_satisfied_reasons=[r for r, _ in satisfied_reasons.most_common(5)],
                top_denied_reasons=[r for r, _ in denied_reasons.most_common(5)],
                total_pages=total_pages,
                total_cases_found=total_cases_found,
                reason_confidence=avg_reason_conf,
            )
        else:
            article_line = f"Статья: {article} | " if article else ""
            pagination_line = ""
            if total_pages > 1:
                pagination_line = f"Найдено {total_cases_found} дел ({total_pages} стр.). Обработано {total_all} дел. "

            quality_note = ""
            if avg_reason_conf < 0.6:
                quality_note = "\n⚠️ Низкая уверенность в основаниях: часть дел классифицирована без прямых цитат из судебных актов."

            summary = (
                "СВОДКА ПО ЗАПРОСУ:\n"
                f"Суд: {court} | Период: {period} | {article_line}{pagination_line}Всего верифицировано: {total_verifiable} (из {total_all})\n"
                f"Статистика: {stats}\n"
                f"Топ-2 основания для удовлетворения: {top_satisfied}\n"
                f"Топ-2 основания для отказа: {top_denied}{quality_note}"
            )

        case_list = self.build_case_list(verifiable_decisions)
        if review_decisions:
            case_list += (
                f"\n\n⚠️ ТРЕБУЮТ РУЧНОЙ ПРОВЕРКИ ({len(review_decisions)} дел):\n"
            )
            case_list += self.build_case_list(review_decisions)

        return AnalysisResult(
            summary=summary,
            case_list=case_list,
            total_pages=total_pages,
            total_cases_found=total_cases_found,
        )

    def build_case_list(self, decisions: list[CaseDecision]) -> str:
        return "\n".join(self._format_case(decision) for decision in decisions)

    def _percentage(self, part: int, total: int) -> int:
        if total == 0:
            return 0
        return round(part / total * 100)

    def _format_top_distinct(
        self, primary: Counter[str], secondary: Counter[str], fallback: Counter[str]
    ) -> str:
        if not primary:
            return self._format_fallback_top(fallback)
        exclusive = [
            (reason, count)
            for reason, count in primary.most_common()
            if secondary.get(reason, 0) == 0
        ]
        if exclusive:
            return "; ".join(reason for reason, _ in exclusive[:2])
        ranked = sorted(
            (
                (reason, count, count - secondary.get(reason, 0))
                for reason, count in primary.items()
            ),
            key=lambda item: (item[2], item[1]),
            reverse=True,
        )
        top = [reason for reason, _, score in ranked if score > 0][:2]
        if not top:
            return self._format_fallback_top(fallback)
        return "; ".join(top)

    def _format_fallback_top(self, reasons: Counter[str]) -> str:
        if not reasons:
            return "нет данных"
        return "; ".join(reason for reason, _ in reasons.most_common(2))

    def _meaningful_reasons(self, reasons: tuple[str, ...]) -> tuple[str, ...]:
        filtered = []
        for reason in reasons:
            normalized = reason.strip().lower()
            if normalized in self._PLACEHOLDER_REASONS:
                continue
            filtered.append(reason)
        return tuple(filtered)

    def _is_verifiable(self, decision: CaseDecision) -> bool:
        has_validation_metadata = (
            bool(decision.matched_article)
            or bool(decision.evidence_quote)
            or decision.evidence_tier != EvidenceTier.TIER_D_NO_MATCH
        )
        if has_validation_metadata:
            return decision.validation_confidence in (
                ConfidenceScore.CONFIRMED,
                ConfidenceScore.PROBABLE,
            )
        return decision.confidence_score >= 0.98

    def _format_case(self, decision: CaseDecision) -> str:
        case_label = decision.case_number or (
            f"ID:{decision.case_id}" if decision.case_id else "Номер дела не указан"
        )
        court = decision.court_name or "Суд не указан"
        reason = self._format_reason(decision)
        link = decision.case_link or "https://kad.arbitr.ru/"

        quote_text = ""
        if decision.proof_quote:
            quote_text = f" | Цитата: {decision.proof_quote}"
        if decision.evidence_quote and decision.evidence_quote != "N/A":
            quote_text = f" | Цитата: {decision.evidence_quote}"

        confidence_display = (
            decision.validation_confidence.value
            if hasattr(decision, "validation_confidence")
            else "Unknown"
        )
        article_display = (
            decision.matched_article
            if hasattr(decision, "matched_article") and decision.matched_article
            else "N/A"
        )
        tier_display = (
            decision.evidence_tier.name if hasattr(decision, "evidence_tier") else "N/A"
        )

        validation_text = f" | Анализ: Уверенность: {confidence_display}, Статья: {article_display} ({tier_display})"

        docs_text = ""
        if decision.document_links:
            doc_links = [f"[{d['name']}]({d['url']})" for d in decision.document_links]
            docs_text = " | Документы: " + ", ".join(doc_links)

        return (
            f"{case_label} | {self._format_date(decision.decision_date)} | "
            f"{self._format_outcome(decision)} | Суд: {court} | Основание: {reason} | "
            f"Ссылка: {link}{quote_text}{validation_text}{docs_text}"
        )

    def _format_date(self, value: date) -> str:
        return value.strftime("%d.%m.%Y")

    def _format_outcome(self, decision: CaseDecision) -> str:
        normalized = self.normalize_outcome(decision)
        if normalized == CaseOutcome.SATISFIED:
            return "Удовлетворено"
        if normalized == CaseOutcome.DENIED:
            return "Отказано"
        return "Не определено"

    def normalize_outcome(self, decision: CaseDecision) -> CaseOutcome:
        outcome = decision.outcome
        if isinstance(outcome, CaseOutcome):
            return outcome
        normalized = str(outcome).strip().lower()
        denied_variants = {
            CaseOutcome.DENIED.value,
            "отказано",
            "отказать",
            "прекратить производство",
            "необоснованно",
            "без удовлетворения",
            "оставить без изменения",
            "оставлено без изменения",
            "без изменения",
            "оставить без удовлетворения",
            "оставлено без удовлетворения",
            "не обоснована",
            "жалоба не обоснована",
        }
        if any(v in normalized for v in denied_variants):
            return CaseOutcome.DENIED

        satisfied_variants = {
            CaseOutcome.SATISFIED.value,
            "удовлетворено",
            "удовлетворить",
            "удовлетворить частично",
            "частично удовлетворено",
            "удовлетворено частично",
            "признать незаконным",
            "признать недействительным",
            "обоснованно",
            "жалоба удовлетворена",
            "заявление удовлетворено",
            "жалоба обоснована",
            "отменить",
            "изменить",
            "включить в реестр",
            "взыскать",
        }
        if any(v in normalized for v in satisfied_variants):
            return CaseOutcome.SATISFIED

        return CaseOutcome.UNKNOWN

    def _format_reason(self, decision: CaseDecision) -> str:
        if not decision.reasons:
            return "не указано"
        reasons_text = "; ".join(decision.reasons[:5])
        if decision.reason_confidence < 0.5:
            reasons_text += " [~]"
        return reasons_text

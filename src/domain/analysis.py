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
            summary = "Детальная сводка не сформирована."

        article_line = f" | Статья: {article}" if article else ""
        header = (
            "СВОДКА ПО ЗАПРОСУ:\n"
            f"Суд: {court} | Период: {period}{article_line} | Всего дел: {total_cases_found} | Проверено: {total_verifiable}\n"
            f"Статистика: {stats}\n\n"
        )
        summary = header + summary

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
            decisions=tuple(verifiable_decisions + review_decisions)
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
            allowed = [
                ConfidenceScore.CONFIRMED,
                ConfidenceScore.PROBABLE,
                ConfidenceScore.WEAK,
            ]
            return decision.validation_confidence in allowed
        return decision.confidence_score >= 0.98

    def _format_case(self, decision: CaseDecision) -> str:
        case_label = (decision.case_number or (f"ID:{decision.case_id}" if decision.case_id else "Номер дела не указан")).replace("|", "/")
        court = (decision.court_name or "Суд не указан").replace("|", "/")
        reason = self._format_reason(decision).replace("|", "/").replace("\n", " ").replace("\r", " ")
        link = (decision.case_link or "https://sudact.ru/").replace("|", "/")
        outcome = self._format_outcome(decision)
        d_date = self._format_date(decision.decision_date)

        parts = [
            f"{case_label}",
            f"{d_date}",
            f"{outcome}",
            f"Суд: {court}",
            f"Основание: {reason}",
            f"Ссылка: {link}",
        ]

        quote = ""
        if decision.evidence_quote and decision.evidence_quote != "N/A":
            quote = decision.evidence_quote
        elif decision.proof_quote:
            quote = decision.proof_quote
        
        if quote:
            parts.append(f"Цитата: {quote.replace('|', '/').replace(chr(10), ' ').replace(chr(13), ' ')}")
        
        if decision.decisive_act_title:
            parts.append(f"Акт: {decision.decisive_act_title.replace('|', '/')}")
        if decision.decisive_act_type:
            parts.append(f"Тип акта: {decision.decisive_act_type.replace('|', '/')}")
        if decision.pdf_status and decision.pdf_status != "not_requested":
            parts.append(f"PDF: {decision.pdf_status.replace('|', '/')}")
        if decision.verification_failure_code:
            parts.append(f"Проверка: {decision.verification_failure_code.replace('|', '/')}")
        
        confidence_display = decision.validation_confidence.value
        article_display = decision.matched_article or "N/A"
        tier_display = decision.evidence_tier.name
        parts.append(f"Анализ: Уверенность: {confidence_display}, Статья: {article_display} ({tier_display})")

        if decision.document_links:
            doc_links = [f"[{d['name']}]({d['url']})".replace("|", "/") for d in decision.document_links]
            parts.append("Документы: " + ", ".join(doc_links))

        return " | ".join(parts).replace("\n", " ").replace("\r", " ")

    def _format_date(self, value: date | str | None) -> str:
        if not value:
            return "не указано"
        if isinstance(value, str):
            return value
        try:
            return value.strftime("%d.%m.%Y")
        except (AttributeError, TypeError):
            return str(value)

    def _format_outcome(self, decision: CaseDecision) -> str:
        normalized = self.normalize_outcome(decision)
        if normalized == CaseOutcome.SATISFIED:
            return "Удовлетворено"
        if normalized == CaseOutcome.DENIED:
            return "Отказано"
        return "Не определено"

    def normalize_outcome(self, decision: CaseDecision) -> CaseOutcome:
        if decision.decisive_act_type and decision.decisive_act_type != "merits_act":
            return CaseOutcome.UNKNOWN
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

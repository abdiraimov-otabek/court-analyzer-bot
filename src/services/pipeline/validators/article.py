from __future__ import annotations

import re
from typing import Optional, Tuple

from src.domain.entities import EvidenceTier


class ArticleValidator:
    """
    Validates relevance against decisive-act PDF text.
    Metadata-only or title-only matches must not become verified article matches.
    """

    def __init__(
        self,
        target_article: Optional[str],
        target_paragraph: Optional[str] = None,
        target_part: Optional[str] = None,
        target_subparagraph: Optional[str] = None,
        law_family: Optional[str] = None,
        law_display_name: Optional[str] = None,
        issue_phrase: Optional[str] = None,
    ):
        self.target_article = target_article
        self.target_paragraph = target_paragraph
        self.target_part = target_part
        self.target_subparagraph = target_subparagraph
        self.law_family = law_family
        self.law_display_name = law_display_name
        self.issue_phrase = issue_phrase

    def validate(
        self, text: str, llm_proof_quote: str = "", raw_article: str = ""
    ) -> Tuple[EvidenceTier, str, str]:
        if not self.target_article:
            return EvidenceTier.TIER_D_NO_MATCH, "No Article Requested", "N/A"

        original_text = text or ""
        normalized_text = original_text.lower()
        supplemental_quote = llm_proof_quote.strip()
        normalized_raw_article = (raw_article or "").lower().strip()

        # If the source card explicitly points to a different article/law anchor,
        # treat it as a strong negative signal. This helps filter cases where
        # target article is only mentioned in passing.
        if normalized_raw_article:
            has_target_in_raw = self._has_exact_article_match(normalized_raw_article)
            looks_like_specific_other_anchor = bool(
                re.search(r"(ст\.?|статья|апк|гк|нк|ук|коап|\d)", normalized_raw_article)
            )
            if looks_like_specific_other_anchor and not has_target_in_raw:
                return (
                    EvidenceTier.TIER_D_NO_MATCH,
                    self._reference_label(),
                    "Карточка дела указывает на иную норму, не соответствующую запросу",
                )

        if not self._has_exact_article_match(normalized_text):
            if supplemental_quote and self._has_exact_article_match(supplemental_quote.lower()):
                if self._law_matches(supplemental_quote.lower()) and not self._is_context_dissonant(
                    supplemental_quote.lower()
                ):
                    return (
                        EvidenceTier.TIER_B_PROBABLE_MATCH,
                        self._reference_label(),
                        supplemental_quote,
                    )
            if self._has_substantive_bankruptcy_match(normalized_text):
                return (
                    EvidenceTier.TIER_B_PROBABLE_MATCH,
                    self._reference_label(),
                    self._extract_substantive_snippet(original_text),
                )
            return (
                EvidenceTier.TIER_D_NO_MATCH,
                self._reference_label(),
                "Нет подтверждения статьи в решающем судебном акте",
            )

        if self._requires_scope_match() and not self._scope_matches(normalized_text):
            return (
                EvidenceTier.TIER_D_NO_MATCH,
                self._reference_label(),
                "Не подтверждены часть/пункт статьи в решающем судебном акте",
            )

        if not self._law_matches(normalized_text):
            return (
                EvidenceTier.TIER_D_NO_MATCH,
                self._reference_label(),
                "Не подтвержден закон/кодекс в решающем судебном акте",
            )

        if self._is_context_dissonant(normalized_text):
            return (
                EvidenceTier.TIER_D_NO_MATCH,
                self._reference_label(),
                "Ссылка на статью носит фоновый или технический характер",
            )

        snippet = self._extract_snippet(original_text)
        tier = (
            EvidenceTier.TIER_A_EXPLICIT_MATCH
            if snippet and snippet != "N/A"
            else EvidenceTier.TIER_B_PROBABLE_MATCH
        )
        return tier, self._reference_label(), snippet

    def _has_substantive_bankruptcy_match(self, text: str) -> bool:
        if self.target_article != "61.2":
            return False
        if not self._law_matches(text):
            return False
        if self._is_context_dissonant(text):
            return False

        strong_patterns = [
            r"оспариван\w+\s+сделк\w+",
            r"признан\w+\s+недействительн\w+\s+сделк\w+",
            r"неравноценн\w+",
            r"встречн\w+\s+исполнен\w+",
            r"причинен\w+\s+вред",
            r"подозрительн\w+\s+сделк\w+",
            r"аффилированн\w+",
        ]
        return any(re.search(pattern, text, re.I) for pattern in strong_patterns)

    def _extract_substantive_snippet(self, text: str) -> str:
        patterns = [
            r"оспариван\w+\s+сделк\w+",
            r"признан\w+\s+недействительн\w+\s+сделк\w+",
            r"неравноценн\w+",
            r"встречн\w+\s+исполнен\w+",
            r"причинен\w+\s+вред",
            r"подозрительн\w+\s+сделк\w+",
        ]
        for pattern in patterns:
            match = re.search(rf".{{0,120}}{pattern}.{{0,160}}", text, re.I | re.S)
            if match:
                return f"...{' '.join(match.group(0).split())}..."
        return self._extract_snippet(text)

    def _reference_label(self) -> str:
        parts = []
        if self.target_subparagraph:
            parts.append(f"подп. {self.target_subparagraph}")
        if self.target_paragraph:
            parts.append(f"п. {self.target_paragraph}")
        if self.target_part:
            parts.append(f"ч. {self.target_part}")
        if self.target_article:
            parts.append(f"ст. {self.target_article}")
        if self.law_display_name:
            parts.append(self.law_display_name)
        return " ".join(parts).strip() or "Статья"

    def _requires_scope_match(self) -> bool:
        return bool(self.target_part or self.target_paragraph or self.target_subparagraph)

    def _scope_matches(self, text: str) -> bool:
        checks = []
        if self.target_part:
            checks.append(re.search(rf"ч\.?\s*{re.escape(self.target_part)}\b", text))
        if self.target_paragraph:
            checks.append(re.search(rf"п\.?\s*{re.escape(self.target_paragraph)}\b", text))
        if self.target_subparagraph:
            checks.append(
                re.search(
                    rf"подп?\.?\s*[«\"]?{re.escape(self.target_subparagraph)}[»\"]?\b",
                    text,
                )
            )
        return all(checks)

    def _has_exact_article_match(self, text: str) -> bool:
        compact = re.escape((self.target_article or "").replace(",", "."))
        patterns = [
            rf"ст\.?\s*{compact}(?![\d.])",
            rf"стать[ьяеи]\s*{compact}(?![\d.])",
            rf"(?<!\d){compact}(?!\d)",
        ]
        return any(re.search(pattern, text) for pattern in patterns)

    def _law_matches(self, text: str) -> bool:
        if not self.law_display_name and not self.law_family:
            return True
        lowered = text.lower()
        if self.law_family == "127-ФЗ":
            return "банкрот" in lowered or "несостоятельност" in lowered or "127-фз" in lowered
        if self.law_family == "ГК РФ":
            return "гк рф" in lowered or "гражданск" in lowered
        if self.law_family == "АПК РФ":
            return "апк рф" in lowered or "арбитражн" in lowered and "процессуальн" in lowered
        if self.law_family == "ГПК РФ":
            return "гпк рф" in lowered or "гражданск" in lowered and "процессуальн" in lowered
        if self.law_family == "НК РФ":
            return "нк рф" in lowered or "налогов" in lowered
        if self.law_family == "УК РФ":
            return "ук рф" in lowered or "уголовн" in lowered
        if self.law_family == "КоАП РФ":
            return "коап" in lowered or "административн" in lowered
        if self.law_display_name:
            tokens = [
                token
                for token in re.findall(r"[а-яёa-z0-9-]+", self.law_display_name.lower(), re.I)
                if len(token) >= 3 and token not in {"закона", "закон", "рф"}
            ]
            if not tokens:
                return True
            matched = sum(1 for token in tokens if token in lowered)
            return matched >= max(1, len(tokens) // 2)
        return True

    def _is_context_dissonant(self, text: str) -> bool:
        """
        Heuristic to detect if the article is mentioned as background/procedural 'noise'
        rather than being applied to the merits of the case.
        Returns True if the context suggests it's NOT a merit-based application.
        """
        article = re.escape((self.target_article or "").replace(",", "."))
        
        # 1. Positive patterns: phrases that indicate the article IS applied/decisive
        application_patterns = [
            rf"руководствуясь\s+(?:ч\.?\s*\d+\s+)?ст\.?\s*{article}",
            rf"в\s+соответствии\s+co?\s+ст\.?\s*{article}",
            rf"на\s+основании\s+ст\.?\s*{article}",
            rf"применяя\s+ст\.?\s*{article}",
            rf"предусмотренном?\s+ст\.?\s*{article}",
        ]
        
        # If we see a strong application phrase, it's NOT dissonant
        if any(re.search(p, text, re.I) for p in application_patterns):
            return False

        # 2. Negative patterns: phrases that indicate procedural/technical context
        dissonant_patterns = [
            rf"в\s+силу\s+ст\.?\s*{article}\s+апк",  # procedural reference to APC
            rf"ч\.?\s*\d+\s+ст\.?\s*\d+\s+и\s+ст\.?\s*{article}\s+апк",
            rf"порядок,?\s+предусмотренный\s+ст\.?\s*{article}", # procedural order
        ]

        # If it's explicitly procedural noise, it IS dissonant
        if any(re.search(p, text, re.I) for p in dissonant_patterns):
            return True

        # 3. Bankruptcy-specific noise filtering for article 61.3:
        # case texts often include a generic list "ст. 61.2 и 61.3" while
        # actually deciding under a different norm (e.g., 61.11/61.14).
        if self.target_article == "61.3":
            list_reference_patterns = [
                r"включая\s+сделк\w+\s*,?\s*указан\w+\s+в\s+стать\w+\s*61\.2\s+и\s+61\.3",
                r"стать\w+\s*61\.2\s+и\s+61\.3\s+закона\s+о\s+банкротств",
                r"ст\.\s*61\.2\s+и\s+61\.3\s+закона\s+о\s+банкротств",
            ]
            has_list_reference = any(re.search(p, text, re.I) for p in list_reference_patterns)
            has_core_613_context = any(
                marker in text
                for marker in (
                    "предпочтен",
                    "предпочтительн",
                    "сделка с предпочтением",
                )
            )
            if has_list_reference and not has_core_613_context:
                return True

        # 4. Check for issue phrase as an override
        issue_tokens = [
            token
            for token in (self.issue_phrase or "").lower().split()
            if len(token) >= 4
        ]
        if issue_tokens and any(token in text for token in issue_tokens):
            return False
            
        # Default for matches that don't hit strong indicators: 
        # assume not dissonant for now to avoid false negatives.
        return False

    def _extract_snippet(self, text: str) -> str:
        compact = re.escape((self.target_article or "").replace(",", "."))
        patterns = [
            rf".{{0,120}}(?:ст\.?|стать[ьяеи])\s*{compact}(?![\d.]).{{0,160}}",
            rf".{{0,120}}(?<!\d){compact}(?!\d).{{0,160}}",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.I | re.S)
            if match:
                snippet = " ".join(match.group(0).split())
                return f"...{snippet}..."
        return "N/A"

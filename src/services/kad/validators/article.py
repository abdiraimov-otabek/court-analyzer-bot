import re
from typing import Optional, Tuple

from src.domain.entities import EvidenceTier


class ArticleValidator:
    """
    Scans the actual document text or LLM proof quote to determine
    if the target law article is genuinely present.
    Assigns an EvidenceTier based on the strength of the match.
    """

    def __init__(
        self, target_article: Optional[str], target_paragraph: Optional[str] = None
    ):
        self.target_article = target_article
        self.target_paragraph = target_paragraph

    def validate(
        self, text: str, llm_proof_quote: str = ""
    ) -> Tuple[EvidenceTier, str, str]:
        if not self.target_article:
            return EvidenceTier.TIER_D_NO_MATCH, "No Article Requested", "N/A"

        text_lower = text.lower()
        quote_lower = llm_proof_quote.lower()

        # 1. Exact explicit matching (Tier A)
        if self._has_exact_match(text_lower):
            snippet = self._extract_snippet(text_lower)
            return EvidenceTier.TIER_A_EXPLICIT_MATCH, self.target_article, snippet

        if self._has_exact_match(quote_lower):
            return (
                EvidenceTier.TIER_A_EXPLICIT_MATCH,
                self.target_article,
                llm_proof_quote,
            )

        # 2. Strong conceptual matching (Tier B)
        # This handles cases where the exact "ст. 61.2" is missing, but "подозрительная сделка" is there.
        if self._has_conceptual_match(text_lower) or self._has_conceptual_match(
            quote_lower
        ):
            fallback_quote = (
                llm_proof_quote
                if llm_proof_quote
                else self._extract_conceptual_snippet(text_lower)
            )
            return (
                EvidenceTier.TIER_B_PROBABLE_MATCH,
                f"Основания {self.target_article}",
                fallback_quote,
            )

        # 3. LLM-confirmed relevance (Tier B)
        # If the LLM confirmed relevance but we don't have an exact match in the text,
        # we trust the LLM and upgrade this to Tier B.
        if "llm подтвердила релевантность" in quote_lower:
            return (
                EvidenceTier.TIER_B_PROBABLE_MATCH,
                f"LLM: {self.target_article}",
                llm_proof_quote,
            )

        # 4. Search Engine Hit ONLY (Tier C fallback)
        # Since we query KAD's full-text search with the article, if it's returned here, 
        # it might exist in the document text, but we haven't found it in the snippets.
        return (
            EvidenceTier.TIER_C_WEAK_MATCH,
            f"Поиск по {self.target_article}",
            "Прямая цитата не найдена в доступных фрагментах",
        )

    def _has_exact_match(self, text: str) -> bool:
        if not self.target_article:
            return False

        compact = re.escape(self.target_article.replace(",", "."))
        patterns = [
            rf"ст\.?\s*{compact}\b",
            rf"стать[ьяи]\s*{compact}\b",
            rf"article\s*{compact}\b",
        ]

        if "." in compact:  # e.g., 61.2
            patterns.append(rf"\b{compact}\b")
        else:
            # For general articles like "168", look for "168 ГК" or "168-й статьи"
            patterns.append(rf"\b{compact}\s*(?:гк|гпк|апк|ук|фз|закон)\b")
            patterns.append(rf"\b{compact}-й\s+стать[ьяи]")

        if self.target_paragraph:
            # Paragraph matching is risky, but we try to find them close to each other
            para = re.escape(self.target_paragraph)
            patterns.append(rf"п\.?\s*{para}.{{0,50}}ст\.?\s*{compact}")
            patterns.append(rf"пункт\s*{para}.{{0,50}}стать[ьяи]\s*{compact}")

        return any(re.search(p, text) for p in patterns)

    def _has_conceptual_match(self, text: str) -> bool:
        if not self.target_article:
            return False

        if not self.target_article:
            return False
        if "61.2" in self.target_article or "61.1" in self.target_article:
            return any(
                k in text
                for k in [
                    "подозрительная сделка",
                    "оспаривание сделки",
                    "признании недействительной сделки",
                    "неравноценное встречное",
                    "причинение вреда имущественным правам",
                ]
            )
        if "61.3" in self.target_article:
            return any(
                k in text
                for k in [
                    "сделка с предпочтением",
                    "оказание предпочтения",
                    "оспаривание сделки",
                    "признании недействительной сделки",
                ]
            )

        return False

    def _extract_snippet(self, text: str) -> str:
        if not self.target_article:
            return "N/A"
        compact = re.escape(self.target_article.replace(",", "."))
        pattern = rf".{{0,60}}(?:ст\.?|стать[ьяи]|\b)\s*{compact}\b.{{0,60}}"
        match = re.search(pattern, text)
        if match:
            return "..." + match.group(0).strip().replace("\n", " ") + "..."
        return "N/A"

    def _extract_conceptual_snippet(self, text: str) -> str:
        if not self.target_article:
            return "N/A"
        if "61.2" in self.target_article:
            match = re.search(
                r".{0,60}(?:подозрительная сделка|оспаривание сделки|недействительной сделки).{0,60}",
                text,
            )
            if match:
                return "..." + match.group(0).strip().replace("\n", " ") + "..."
        if self.target_article and "61.3" in self.target_article:
            match = re.search(
                r".{0,60}(?:сделка с предпочтением|оказание предпочтения).{0,60}", text
            )
            if match:
                return "..." + match.group(0).strip().replace("\n", " ") + "..."
        return "N/A"

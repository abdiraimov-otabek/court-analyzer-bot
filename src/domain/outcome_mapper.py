from __future__ import annotations
import re
from src.domain.entities import CaseOutcome

class OutcomeMapper:
    """
    Maps Russian court decision text to a CaseOutcome (SATISFIED, DENIED, UNKNOWN).
    Based on common arbitration court phrasing.
    """

    def __init__(self) -> None:
        # Complex regex patterns for DENIED outcome
        self._denied_patterns = [
            # 1. Direct refusal with varying word distance
            re.compile(r"отказа\w*\s+(?:в\s+)?удовлетворени\w*", re.IGNORECASE),
            re.compile(r"в\s+удовлетворени\w*(?:\s+\w+){0,10}\s+отказа\w*", re.IGNORECASE),
            
            # 2. Leave without satisfaction or consideration
            re.compile(r"остави\w*(?:\s+\w+)?\s+без\s+(?:удовлетворения|рассмотрения)\w*", re.IGNORECASE),
            re.compile(r"без\s+(?:удовлетворения|рассмотрения)\w*", re.IGNORECASE),
            
            # 3. Refuse lawsuit or recognition
            re.compile(r"отказа\w*\s+в\s+(?:иске|признании)\w*", re.IGNORECASE),
            re.compile(r"в\s+иске\s+отказа\w*", re.IGNORECASE),
            
            # 4. Termination of proceedings
            re.compile(r"прекратить\s+производство", re.IGNORECASE),
            
            # 5. Lack of grounds or markers (Bug 1 fixes)
            re.compile(r"(?:не\s+подлежит|не\s+может\s+быть|не\s+мог\s+быть)\s+признан", re.IGNORECASE),
            re.compile(r"отсутству\w*\s+основания", re.IGNORECASE),
            re.compile(r"(?:не\s+установлены|отсутствуют)\s+признаки", re.IGNORECASE),
            re.compile(r"признать\s+необоснован\w*", re.IGNORECASE),
            
            # 6. Standalone "отказ" (usually at end of sentence)
            re.compile(r"удовлетворени\w+\s+отказ(?:\s|$|\.)", re.IGNORECASE),
        ]

        # Simpler keywords for SATISFIED outcome
        self._satisfied_keywords = [
            "удовлетвор",
            "признать недействит",
            "признано незаконным",
            "признать незаконн",
            "взыскать",
            "привлечь к ответственности",
            "признать ненадлежащим",
            "признана обоснованной",
            "признано обоснованным",
        ]

    def map_outcome(self, text: str) -> CaseOutcome:
        if not text:
            return CaseOutcome.UNKNOWN
            
        lower = text.lower()
        
        # Priority 1: Check for DENIED markers (even if satisfied keywords exist)
        for pattern in self._denied_patterns:
            if pattern.search(lower):
                return CaseOutcome.DENIED
                
        # Priority 2: Check for SATISFIED keywords
        for keyword in self._satisfied_keywords:
            if keyword in lower:
                return CaseOutcome.SATISFIED
                
        return CaseOutcome.UNKNOWN

from src.domain.entities import CaseOutcome, ConfidenceScore, EvidenceTier


class EvidenceScorer:
    """Calculates final confidence based on accumulated evidence."""

    @staticmethod
    def score(
        tier: EvidenceTier, court_match: bool, outcome: CaseOutcome
    ) -> ConfidenceScore:
        if not court_match:
            return ConfidenceScore.REJECTED

        if outcome == CaseOutcome.UNKNOWN:
            # If we don't know the outcome, even a Tier A match cannot be Confirmed.
            # It's weak evidence of practice until outcome is known.
            return ConfidenceScore.WEAK

        if tier == EvidenceTier.TIER_A_EXPLICIT_MATCH:
            return ConfidenceScore.CONFIRMED

        if tier == EvidenceTier.TIER_B_PROBABLE_MATCH:
            return ConfidenceScore.PROBABLE

        if tier == EvidenceTier.TIER_C_WEAK_MATCH:
            return ConfidenceScore.WEAK

        return ConfidenceScore.REJECTED

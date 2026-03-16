import asyncio
from datetime import date
from src.domain.entities import CaseDecision, CaseOutcome, EvidenceTier, ConfidenceScore
from src.domain.analysis import AnalysisService
from src.services.case_exporter import build_cases_excel

dec = CaseDecision(
    case_number="A40-1/2023",
    decision_date=date(2023, 1, 1),
    outcome=CaseOutcome.SATISFIED,
    reasons=("основание 1",),
    court_name="АС МО",
    evidence_quote="Цитата 1\nЦитата 2",
    evidence_tier=EvidenceTier.TIER_A_EXPLICIT_MATCH,
    validation_confidence=ConfidenceScore.CONFIRMED,
    matched_article="723"
)

service = AnalysisService()
case_list = service.build_case_list([dec, dec])
print("CASE LIST:")
print(case_list)
print("----------------")
excel_bytes = build_cases_excel([dec, dec])
print(f"Excel length: {len(excel_bytes)}")

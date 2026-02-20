import json
import os
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.app.config import load_config
from src.domain.entities import CaseOutcome
from src.domain.settings import Settings
from src.services.kad_client import ParserApiKadClient


@dataclass
class Sample:
    case_number: str
    expected_outcome: str
    expected_reasons: list[str]


def load_samples(path: Path) -> list[Sample]:
    data = json.loads(path.read_text(encoding="utf-8"))
    samples: list[Sample] = []
    for item in data:
        samples.append(
            Sample(
                case_number=item["case_number"],
                expected_outcome=item["expected_outcome"],
                expected_reasons=item.get("expected_reasons", []),
            )
        )
    return samples


def normalize_outcome(value: CaseOutcome) -> str:
    if value == CaseOutcome.SATISFIED:
        return "satisfied"
    return "denied"


def evaluate(samples: list[Sample]) -> None:
    config = load_config()
    if not config.kad_api_key:
        raise RuntimeError("KAD_API_KEY is not configured")

    client = ParserApiKadClient(base_url=config.kad_api_base_url, api_key=config.kad_api_key)
    settings = Settings(
        max_cases=1,
        max_documents_per_case=5,
        max_pages=1,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        analysis_prompt="",
        updated_at=datetime.now(),
    )

    total = len(samples)
    outcome_correct = 0
    reason_correct = 0

    for sample in samples:
        result = asyncio.run(client.fetch_decisions(sample.case_number, settings))
        if not result.decisions:
            continue
        decision = result.decisions[0]
        predicted_outcome = normalize_outcome(decision.outcome)
        if predicted_outcome == sample.expected_outcome:
            outcome_correct += 1
        if sample.expected_reasons:
            found = any(
                expected.lower() in " ".join(decision.reasons).lower()
                for expected in sample.expected_reasons
            )
            if found:
                reason_correct += 1
        else:
            reason_correct += 1

    outcome_accuracy = (outcome_correct / total) * 100 if total else 0
    reason_accuracy = (reason_correct / total) * 100 if total else 0

    print(f"Total samples: {total}")
    print(f"Outcome accuracy: {outcome_accuracy:.1f}%")
    print(f"Reason accuracy: {reason_accuracy:.1f}%")


if __name__ == "__main__":
    sample_path = os.getenv("ACCURACY_SAMPLE_PATH", "data/accuracy_samples.json")
    path = Path(sample_path)
    if not path.exists():
        raise FileNotFoundError(f"Sample file not found: {path}")
    samples = load_samples(path)
    if len(samples) < 100:
        raise RuntimeError("Sample size must be at least 100 cases")
    evaluate(samples)

from __future__ import annotations


def current_version_bundle() -> str:
    return (
        "extraction_rules:v1|article_normalization:v1|outcome_classifier:v1|"
        "prompts:v1|evidence_selection:v2|source_adapters:v2"
    )

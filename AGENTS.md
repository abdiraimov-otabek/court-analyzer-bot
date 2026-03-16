# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

## Project Overview

Telegram bot for automated search and statistical analysis of court decisions from the Russian Arbitration Court Case Database (KAD). Users send natural language queries; the bot estimates case volume, fetches case data via an external API, analyzes outcomes, and returns a Russian-language summary plus a file with the case list.

Code is in English. All user-facing text should remain in Russian.

## Commands

```bash
# Install dependencies (uses uv, Python 3.11+)
uv pip install -e .[dev]

# Run all tests
pytest

# Run a focused test file
pytest tests/test_bot_logic.py -v
pytest tests/test_request_processor.py -v

# Run Telegram bot
python -m src.app.run_bot

# Run Admin API
python -m src.app.run_admin
# or
uvicorn src.app.admin_api:app --host 0.0.0.0 --port 8000
```

## Architecture

The project follows a layered structure with framework adapters at the edge and business logic in the middle.

- `src/app/` - entry points and adapters
  - `telegram_bot.py` wires aiogram handlers to `BotLogic` and the async analysis flow.
  - `admin_api.py` exposes FastAPI admin endpoints and the lightweight HTML admin UI.
  - `container.py` wires repositories, services, shared HTTP clients, and application objects.
  - `config.py` loads `.env` from the repo root and validates required environment variables.
  - `bot_logging.py` provides structured logging and request context helpers.
  - `analysis_notifications.py` handles slow-analysis notifications.
- `src/core/` - framework-free orchestration
  - `bot_logic.py` handles command responses, access control, rate limiting, query validation, quarter selection, and the count-to-analysis transition.
  - `estimation.py` estimates analysis duration.
- `src/domain/` - pure domain types and logic
  - `entities.py` contains analysis and case outcome entities.
  - `case_models.py` defines the `CaseClient` protocol and shared data models.
  - `outcome_mapper.py` contains rule-based logic for mapping decision text to outcomes.
  - `settings.py` defines `Settings` and defaults.
  - `value_objects.py` defines validated immutable inputs such as `UserId` and `QueryText`.
  - `analysis.py` builds the summary and case-list text.
  - `reason_extractor.py` contains rule-based reason extraction.
- `src/services/` - application services and business workflows
  - `sudact_client.py` is the main adapter for fetching decisions. It scrapes `sudact.ru` directly.
  - `request_processor.py` orchestrates cache lookup, collection, validation, analysis, and logging.
  - `query_parser.py` and `query_validator.py` parse and validate natural-language requests.
  - `quarter_selection.py`, `active_requests.py`, `access_control.py`, and `rate_limit.py` implement per-user guards and state.
  - `case_exporter.py`, `hashing.py`, `llm_reason_extractor.py`, and `settings_service.py` support export, privacy, LLM enrichment, and settings updates.
- `src/infrastructure/` - SQLite persistence
  - `sqlite.py` owns schema bootstrap, WAL configuration, and lightweight migrations.
  - Repository classes persist settings, access lists, active requests, caches, logs, and admin sessions.

## Key Flows

- Protocol-based dependency injection: core logic depends on protocols such as `CountProvider` and `SettingsProvider`, which keeps tests simple.
- Single active request per user: requests move through `counting -> collecting -> analyzing`; `/status` and `/cancel` rely on that lifecycle.
- Quarter-selection flow: when the estimated result set exceeds `max_cases`, the bot stores a pending quarter choice and asks the user to narrow the query.
- Split sync/async handling: `BotLogic.pre_validate()` plus `apply_count_result()` support the async Telegram flow, while `handle_message()` still covers synchronous command handling.
- Request processing pipeline: `RequestProcessor` performs cache lookup, collection, validation, quality gates, summary generation, and audit logging.
- Caching exists at two levels: full analysis results and per-case details.
- Admin settings are cross-cutting: changing settings usually affects the domain model, SQLite schema/migrations, repository mapping, admin API models/forms, and tests.

## Environment Variables

Loaded from `.env` at the project root.

- Core variables: `TELEGRAM_BOT_TOKEN`, `KAD_API_BASE_URL`, `KAD_API_KEY`, `ADMIN_AUTH_TOKEN`, `HASH_SALT`, `DATABASE_PATH`
- LLM variable: `OPENROUTER_API_KEY`

Important: `src/app/config.py` currently validates `OPENROUTER_API_KEY` as required at startup. If you intend it to be optional, update the validation logic and any codepaths that assume it is present.

## Testing

Tests live in `tests/` and are run with `pytest`. Most unit tests use in-memory stubs or fake collaborators rather than real external services.

When changing specific areas, run the closest tests first:

- Bot flow and guards: `tests/test_bot_logic.py`, `tests/test_active_requests.py`, `tests/test_rate_limit.py`
- Query parsing/validation: `tests/test_query_parser.py`, `tests/test_query_validator.py`
- KAD fetching and adaptive behavior: `tests/test_kad_client.py`, `tests/test_kad_client_adaptive.py`
- End-to-end processing and quality gates: `tests/test_request_processor.py`
- Settings/admin persistence: `tests/test_settings_repo.py`, `tests/test_settings_service.py`, `tests/test_container.py`

There are also accuracy and golden-style tests in the suite. If behavior changes intentionally, check whether the expected outputs need to move with the implementation.

## Working Conventions

- Keep user-facing strings in Russian.
- Keep framework-specific code in `src/app/`; avoid pulling FastAPI or aiogram concerns into `src/core/` or `src/domain/`.
- Preserve the single-active-request behavior unless the change is deliberate and coordinated across bot logic, request processing, and tests.
- When adding or changing a setting, review:
  - `src/domain/settings.py`
  - `src/infrastructure/sqlite.py`
  - `src/infrastructure/settings_repository.py`
  - `src/services/settings_service.py`
  - `src/app/admin_api.py`
  - related tests under `tests/`
- When changing KAD fetch or validation behavior, review:
  - `src/services/kad_client.py`
  - `src/services/kad/pipeline.py`
  - `src/services/kad/validators/`
  - `src/services/request_processor.py`
  - related tests under `tests/`

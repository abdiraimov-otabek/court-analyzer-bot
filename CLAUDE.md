# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram bot for automated search and statistical analysis of court decisions from the Russian Arbitration Court Case Database (КАД/KAD). Users send natural language queries; the bot estimates case volume, fetches case data via an external API (parser-api.com), analyzes outcomes, and returns a summary + file listing.

Language: Russian-language UI, English code. All user-facing strings are in Russian.

## Commands

```bash
# Install dependencies (uses uv, Python 3.11+)
uv pip install -e .[dev]

# Run all tests
pytest

# Run a single test
pytest tests/test_bot_logic.py
pytest tests/test_bot_logic.py::test_function_name -v

# Run Telegram bot
python -m src.app.run_bot

# Run Admin API
python -m src.app.run_admin
# or: uvicorn src.app.admin_api:app --host 0.0.0.0 --port 8000
```

## Architecture

**Layered structure** with clear separation:

- **`src/app/`** — Entry points and framework adapters
  - `telegram_bot.py` — aiogram handlers, wires user messages to `BotLogic`
  - `admin_api.py` — FastAPI admin endpoints (settings, users, logs)
  - `container.py` — DI container, wires all dependencies
  - `config.py` — loads `.env` via python-dotenv into `AppConfig` dataclass
  - `logging.py` — structured logging with `log_event`/`log_debug` helpers

- **`src/core/`** — Business orchestration (no framework deps)
  - `bot_logic.py` — Main message handler: access control → rate limit → active request check → query validation → count → quarter selection or analysis. Has both sync (`handle_message`) and async-friendly (`pre_validate` + `apply_count_result`) flows
  - `estimation.py` — Estimates analysis duration from case count

- **`src/domain/`** — Pure domain types
  - `entities.py` — `CaseDecision`, `CaseOutcome`, `AnalysisResult`
  - `value_objects.py` — `UserId`, `QueryText` (validated, immutable)
  - `settings.py` — `Settings` dataclass (max_cases, concurrency, thresholds)
  - `analysis.py` — `AnalysisService` (builds summary + case list text)
  - `reason_extractor.py` — Rule-based reason extraction from court texts

- **`src/services/`** — Business logic services
  - `kad_client.py` — `ParserApiKadClient`: HTTP adapter for external KAD API with adaptive concurrency, case details caching, and retry logic. Uses both sync (`count_cases`) and async (`fetch_decisions`) HTTP clients
  - `request_processor.py` — Orchestrates full analysis pipeline (fetch → analyze → cache → log)
  - `rate_limit.py`, `access_control.py`, `active_requests.py` — Per-user guards
  - `llm_reason_extractor.py` — LLM-based reason extraction via OpenRouter API
  - `cache.py`, `hashing.py`, `case_exporter.py`, `query_validator.py`

- **`src/infrastructure/`** — SQLite persistence
  - `sqlite.py` — Connection wrapper with auto-schema migration (WAL mode)
  - `*_repository.py` — Repository classes for each table

## Key Patterns

- **Protocol-based DI**: Core logic depends on Protocols (`CountProvider`, `SettingsProvider`, `KadClient`), not concrete classes. Tests use simple stubs.
- **Single active request per user**: Enforced by `ActiveRequestRegistry`. Requests go through phases: `counting` → `collecting` → `analyzing`.
- **Quarter selection flow**: When case count exceeds `max_cases`, bot asks user to pick a quarter (1-4) to narrow the search.
- **Dual sync/async**: `kad_client` supports both sync (`count_cases` for quick counts) and async (`fetch_decisions` for parallel fetching).
- **SQLite with WAL**: All persistent state in one SQLite DB. Schema auto-migrates on startup via `_ensure_schema` and `_ensure_settings_columns`.

## Environment Variables

Loaded from `.env` at project root. Key vars: `TELEGRAM_BOT_TOKEN`, `KAD_API_BASE_URL`, `KAD_API_KEY`, `ADMIN_AUTH_TOKEN`, `HASH_SALT`, `DATABASE_PATH` (default: `data/app.db`), `OPENROUTER_API_KEY` (optional, for LLM extraction).

## Testing

Tests are in `tests/` using pytest. Most tests use in-memory stubs — no external services needed. `pythonpath = ["."]` is set in `pyproject.toml` so imports work as `from src.xxx import yyy`.

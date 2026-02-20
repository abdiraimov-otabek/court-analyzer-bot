# Audit Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix every issue identified in the engineering audit — security, reliability, cost, correctness, and UX — while keeping all 152 existing tests green.

**Architecture:** Fixes are grouped by risk: data model correctness first, database layer second, LLM layer third, admin API security fourth, then active-request observability. Each group is independently committable. No external dependencies added.

**Tech Stack:** Python 3.12, asyncio, SQLite (via stdlib sqlite3), httpx, aiogram 3, FastAPI, openpyxl

---

## Task 1: Fix `CaseDecision.reasons` — mutable list inside frozen dataclass

**Why:** `frozen=True` prevents field rebinding but not `decision.reasons.append(x)`. Use `tuple[str, ...]` to enforce true immutability.

**Files:**
- Modify: `src/domain/entities.py`
- Modify: `src/domain/reason_extractor.py`
- Modify: `src/services/llm_reason_extractor.py`
- Modify: `src/services/kad_client.py` (3 spots where reasons are built/compared)
- Modify: `src/infrastructure/case_details_cache_repository.py`
- Tests auto-verify via existing suite

**Step 1: Update `entities.py`**

```python
# src/domain/entities.py
@dataclass(frozen=True)
class CaseDecision:
    case_number: str
    decision_date: date
    outcome: CaseOutcome
    reasons: tuple[str, ...]          # ← was list[str]
    case_id: str = ""
    court_name: str = ""
    case_link: str = ""
    analysis_text: str = ""
    case_category: str = ""
```

**Step 2: Update `reason_extractor.py` — change return type and all `return` statements**

```python
# src/domain/reason_extractor.py
def extract(self, text: str) -> tuple[str, ...]:   # ← was list[str]
    ...
    # Every `return reasons` or `return []` becomes:
    return tuple(reasons)
    # The final fallback:
    return ("оценка обстоятельств дела",)
```

**Step 3: Update `llm_reason_extractor.py` — class constants and return types**

```python
# src/services/llm_reason_extractor.py
_FALLBACK: tuple[str, ...] = ("оценка обстоятельств дела",)
_CANONICAL_SET: frozenset[str] = frozenset()   # populated at class body level (see Task 3)

# All methods returning list[str] → tuple[str, ...]
async def extract(self, text: str, outcome: CaseOutcome) -> tuple[str, ...]:
    ...
async def _call_api(self, text: str, outcome: CaseOutcome) -> tuple[str, ...]:
    ...
    valid = tuple(r for r in raw if r in self._CANONICAL_SET or r == self._FALLBACK[0])
    return valid if valid else self._FALLBACK
```

**Step 4: Update `kad_client.py` — reasons comparisons and construction**

Find all 3 places:
1. `_extract_outcome_and_reasons` — the final fallback and the `replace()` call:
```python
# kad_client.py:_extract_outcome_and_reasons
reasons: tuple[str, ...] = self._reason_extractor.extract(analysis_text)
if not reasons and decisive_idx > 0:
    fallback_text = prepared[decisive_idx - 1][0]
    reasons = self._reason_extractor.extract(fallback_text)
    ...
if not reasons:
    reasons = ("оценка обстоятельств дела",)
return outcome, reasons, decision_date, analysis_text
```

2. LLM fallback check in `_fetch_case_decision_with_metrics`:
```python
# Change the comparison:
if decision.reasons == ("оценка обстоятельств дела",):
```

3. LLM fallback check in `_fetch_case_by_number_async`:
```python
if decision.reasons == ("оценка обстоятельств дела",):
```

**Step 5: Update `case_details_cache_repository.py` — deserialization**

```python
# _deserialize_decision
reasons=tuple(data.get("reasons", [])),
```

**Step 6: Run tests**

```bash
uv run pytest tests/ -q
```
Expected: 152 passed. Many tests compare `reasons == ["..."]` or similar — those will fail and need updating to use `("...",)` or `== ("оценка обстоятельств дела",)`.

**Step 7: Fix all test comparisons**

Search for list-based reasons comparisons in tests:
```bash
grep -n 'reasons.*\[' tests/
grep -n '\["оценка' tests/
```
Update each to `("оценка обстоятельств дела",)` or `("label1", "label2")`.

**Step 8: Run tests again**

```bash
uv run pytest tests/ -q
```
Expected: 152 passed.

**Step 9: Commit**

```bash
git add src/domain/entities.py src/domain/reason_extractor.py \
        src/services/llm_reason_extractor.py src/services/kad_client.py \
        src/infrastructure/case_details_cache_repository.py tests/
git commit -m "fix: make CaseDecision.reasons immutable tuple[str, ...]"
```

---

## Task 2: SQLite — enable WAL mode and close connections properly

**Why:** Without WAL, concurrent writes from the bot process and the admin API process cause `database is locked` errors. The `connect()` method also leaks file descriptors by never calling `conn.close()`.

**Files:**
- Modify: `src/infrastructure/sqlite.py`
- Test: `tests/test_cache.py` (check it still passes — no new test needed, this is plumbing)

**Step 1: Rewrite `SqliteConnection.connect()` as a context manager**

```python
# src/infrastructure/sqlite.py
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


class SqliteConnection:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        self._db_path = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()          # ← explicit close, no FD leak
```

> **Note:** All call sites already use `with self._connection.connect() as conn:` — this is a **drop-in** change. No call sites need updating.

**Step 2: Run the full test suite to confirm nothing broke**

```bash
uv run pytest tests/ -q
```
Expected: 152 passed.

**Step 3: Commit**

```bash
git add src/infrastructure/sqlite.py
git commit -m "fix: SQLite WAL mode + explicit connection close (no more FD leaks or write contention)"
```

---

## Task 3: `LLMReasonExtractor` — three improvements: class-level set, result cache, per-fetch budget, retry

**Why:**
- `_CANONICAL_SET = None` lazy init is a code smell; use `frozenset` directly.
- `temperature=0` → deterministic output → cache hits eliminate 90%+ of redundant API calls.
- 500 cases with all-generic reasons = 500 LLM calls ≈ $0.50 surprise bill → budget cap.
- Transient 429/503 from OpenRouter silently returns fallback; should retry 1× before giving up.

**Files:**
- Modify: `src/services/llm_reason_extractor.py`
- Modify: `tests/test_llm_reason_extractor.py`

**Step 1: Write new tests first**

Add to `tests/test_llm_reason_extractor.py`:

```python
@pytest.mark.asyncio
async def test_cache_hit_skips_api_call():
    """Second call with identical input must not call the API."""
    http = AsyncMock()
    canonical = "пропуск срока исковой давности"
    http.post = AsyncMock(
        return_value=_mock_response(_api_body(json.dumps([canonical], ensure_ascii=False)))
    )
    extractor = _make_extractor(http_client=http)

    r1 = await extractor.extract("текст дела", CaseOutcome.DENIED)
    r2 = await extractor.extract("текст дела", CaseOutcome.DENIED)   # identical

    assert r1 == r2 == (canonical,)
    assert http.post.call_count == 1   # only called once


@pytest.mark.asyncio
async def test_budget_cap_stops_calls():
    """After max_llm_calls_per_fetch are used, extract returns fallback without calling API."""
    http = AsyncMock()
    canonical = "аффилированность сторон"
    http.post = AsyncMock(
        return_value=_mock_response(_api_body(json.dumps([canonical], ensure_ascii=False)))
    )
    extractor = _make_extractor(http_client=http)
    extractor.set_fetch_budget(max_calls=2)

    results = [await extractor.extract(f"text {i}", CaseOutcome.DENIED) for i in range(5)]

    assert http.post.call_count == 2
    assert results[0] == results[1] == (canonical,)
    assert results[2] == results[3] == results[4] == ("оценка обстоятельств дела",)


@pytest.mark.asyncio
async def test_retry_on_429_succeeds():
    """A single 429 response should be retried once and succeed."""
    import httpx
    http = AsyncMock()
    canonical = "мнимость сделки (ст.170 ГК)"
    rate_limit_resp = MagicMock()
    rate_limit_resp.status_code = 429
    rate_limit_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)
    )
    ok_resp = _mock_response(_api_body(json.dumps([canonical], ensure_ascii=False)))
    http.post = AsyncMock(side_effect=[rate_limit_resp, ok_resp])
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("текст", CaseOutcome.DENIED)

    assert result == (canonical,)
    assert http.post.call_count == 2


@pytest.mark.asyncio
async def test_budget_resets_between_fetches():
    """After reset_fetch_budget(), calls are allowed again."""
    http = AsyncMock()
    canonical = "безвозмездность сделки"
    http.post = AsyncMock(
        return_value=_mock_response(_api_body(json.dumps([canonical], ensure_ascii=False)))
    )
    extractor = _make_extractor(http_client=http)
    extractor.set_fetch_budget(max_calls=1)

    await extractor.extract("text a", CaseOutcome.DENIED)   # uses 1 call
    await extractor.extract("text b", CaseOutcome.DENIED)   # blocked by budget
    assert http.post.call_count == 1

    extractor.reset_fetch_budget()                          # reset
    await extractor.extract("text b", CaseOutcome.DENIED)   # allowed again
    assert http.post.call_count == 2
```

**Step 2: Run new tests — confirm they FAIL**

```bash
uv run pytest tests/test_llm_reason_extractor.py -k "cache_hit or budget_cap or retry_on_429 or budget_resets" -v
```
Expected: 4 FAILED (methods not yet implemented).

**Step 3: Rewrite `llm_reason_extractor.py` with all improvements**

```python
# src/services/llm_reason_extractor.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import httpx

from src.domain.entities import CaseOutcome

logger = logging.getLogger("llm_reason_extractor")


class LLMReasonExtractor:
    _CANONICAL_LABELS: tuple[str, ...] = (
        "неравноценное встречное исполнение (п.1 ст.61.2)",
        "причинение вреда кредиторам (п.2 ст.61.2)",
        "подозрительность сделки (ст.61.2)",
        "сделка с предпочтением (ст.61.3)",
        "нарушение очередности удовлетворения требований",
        "злоупотребление правом (ст.10 ГК)",
        "мнимость сделки (ст.170 ГК)",
        "притворность сделки (ст.170 ГК)",
        "ничтожность сделки (ст.168 ГК)",
        "пропуск срока исковой давности",
        "недоказанность обстоятельств",
        "недостаточность доказательств",
        "необоснованность требований",
        "отсутствие правовых оснований",
        "осведомленность контрагента о банкротстве",
        "аффилированность сторон",
        "заинтересованность контрагента",
        "признаки неплатежеспособности должника",
        "добросовестность контрагента",
        "безвозмездность сделки",
        "оспаривание сделки по ст.61.2 Закона о банкротстве",
        "оспаривание сделки по ст.61.3 Закона о банкротстве",
        "ненадлежащий ответчик",
        "нарушение обязанностей арбитражного управляющего",
        "применение последствий недействительности сделки",
        "крупная сделка",
    )
    _CANONICAL_SET: frozenset[str] = frozenset(_CANONICAL_LABELS)
    _FALLBACK: tuple[str, ...] = ("оценка обстоятельств дела",)
    _LABELS_STR: str = "\n".join(f"- {l}" for l in _CANONICAL_LABELS)

    _MAX_RETRIES = 1          # retry once on 429/503
    _CACHE_MAX_SIZE = 4096    # ~1 MB at ~250 bytes/entry

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        model: str = "openai/gpt-4o-mini",
        timeout: int = 15,
        max_concurrent: int = 8,
    ) -> None:
        self._http_client = http_client
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._result_cache: dict[str, tuple[str, ...]] = {}
        self._budget_remaining: int | None = None   # None = unlimited

    # ── Public budget control (called by kad_client per fetch session) ──────

    def set_fetch_budget(self, max_calls: int) -> None:
        """Cap LLM calls for the current fetch session."""
        self._budget_remaining = max_calls

    def reset_fetch_budget(self) -> None:
        """Reset budget to unlimited (call at the start of each fetch_decisions)."""
        self._budget_remaining = None

    # ── Main entry point ─────────────────────────────────────────────────────

    async def extract(self, text: str, outcome: CaseOutcome) -> tuple[str, ...]:
        if not text or not text.strip():
            return self._FALLBACK

        # Check budget before calling API
        if self._budget_remaining is not None and self._budget_remaining <= 0:
            return self._FALLBACK

        # Check result cache (temperature=0 → deterministic)
        cache_key = self._cache_key(text, outcome)
        if cache_key in self._result_cache:
            return self._result_cache[cache_key]

        try:
            async with self._semaphore:
                result = await self._call_with_retry(text, outcome)
        except Exception:
            return self._FALLBACK

        # Consume budget only on actual API call
        if self._budget_remaining is not None:
            self._budget_remaining -= 1

        # Store in cache (evict oldest if full)
        if len(self._result_cache) >= self._CACHE_MAX_SIZE:
            self._result_cache.pop(next(iter(self._result_cache)))
        self._result_cache[cache_key] = result
        return result

    # ── Internals ────────────────────────────────────────────────────────────

    def _cache_key(self, text: str, outcome: CaseOutcome) -> str:
        return hashlib.sha256(f"{outcome.value}:{text}".encode()).hexdigest()

    async def _call_with_retry(self, text: str, outcome: CaseOutcome) -> tuple[str, ...]:
        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return await self._call_api(text, outcome)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {429, 503} and attempt < self._MAX_RETRIES:
                    last_exc = exc
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    async def _call_api(self, text: str, outcome: CaseOutcome) -> tuple[str, ...]:
        outcome_ru = {"satisfied": "Удовлетворено", "denied": "Отказано"}.get(
            outcome.value, "Не определено"
        )
        prompt = (
            f"Ты анализируешь определение арбитражного суда по делу о банкротстве.\n"
            f"Текст события: {text}\n"
            f"Результат: {outcome_ru}\n\n"
            f"Выбери 1–3 правовых основания из списка ниже, которые наиболее точно "
            f"соответствуют данному делу. Если информации недостаточно, верни "
            f'["оценка обстоятельств дела"].\n\n'
            f"Допустимые значения:\n{self._LABELS_STR}\n\n"
            f"Ответь ТОЛЬКО JSON-массивом строк, без пояснений."
        )
        response = await self._http_client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/kad-bot",
                "X-Title": "KAD Bot",
            },
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 150,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        # GPT-4o-mini sometimes wraps output in ```json ... ``` fences
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        raw = json.loads(content)
        if not isinstance(raw, list):
            return self._FALLBACK
        valid = tuple(r for r in raw if r in self._CANONICAL_SET or r == self._FALLBACK[0])
        return valid if valid else self._FALLBACK
```

**Step 4: Wire budget reset in `kad_client.py` → `fetch_decisions`**

```python
# src/services/kad_client.py: fetch_decisions() — add near the top after sanity checks
if self._llm_reason_extractor is not None:
    self._llm_reason_extractor.set_fetch_budget(max_calls=50)
```

Add a corresponding reset in the finally-equivalent (after the loop):
```python
# No explicit finally needed; the budget is reset at the START of each fetch via set_fetch_budget()
# This ensures each new fetch starts fresh.
```

**Step 5: Run new tests**

```bash
uv run pytest tests/test_llm_reason_extractor.py -v
```
Expected: all tests pass (including the 4 new ones).

**Step 6: Run full suite**

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 7: Commit**

```bash
git add src/services/llm_reason_extractor.py src/services/kad_client.py \
        tests/test_llm_reason_extractor.py
git commit -m "feat: LLM result cache + per-fetch budget cap (50 calls max) + retry on 429/503"
```

---

## Task 4: Rate limiter — use `time.monotonic()` instead of `datetime.now()`

**Why:** `datetime.now()` is wall-clock time and can jump backward on NTP corrections. `time.monotonic()` always increases.

**Note:** The rate limiter currently uses `datetime` objects, not raw floats. The cleanest fix uses `time.monotonic()` throughout.

**Files:**
- Modify: `src/services/rate_limit.py`
- Modify: `src/core/bot_logic.py` (passes `now: datetime` — needs to pass `float` from monotonic, or we keep datetime and just fix internally)
- Modify: `tests/test_rate_limit.py`

**Best approach:** Keep the public `allow(user_id, now: datetime)` API unchanged (callers pass `message.date`), but use `time.monotonic()` internally for the window calculation.

**Step 1: Update `rate_limit.py`**

```python
# src/services/rate_limit.py
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime
import threading

from src.domain.value_objects import UserId


class HourlyRateLimiter:
    def __init__(self, limit: int = 10, window_seconds: int = 3600) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._events: dict[UserId, list[float]] = defaultdict(list)   # monotonic timestamps
        self._lock = threading.Lock()

    def allow(self, user_id: UserId, now: datetime) -> bool:  # now param kept for API compat
        mono_now = time.monotonic()
        with self._lock:
            events = self._events[user_id]
            cutoff = mono_now - self._window_seconds
            while events and events[0] <= cutoff:
                events.pop(0)
            if len(events) >= self._limit:
                return False
            events.append(mono_now)
            return True
```

**Step 2: Run existing rate limiter tests**

```bash
uv run pytest tests/test_rate_limit.py -v
```
Expected: all pass (the tests mock `now` but the implementation now ignores it for window calculation — review tests to make sure they still exercise the right behavior).

**Step 3: If tests broke, update test fixtures**

The tests probably manually control `now` to simulate time passing. Since we now use `time.monotonic()` internally, tests that need to control time must patch `time.monotonic`. Update any such tests:

```python
# In tests/test_rate_limit.py, if needed:
from unittest.mock import patch

def test_window_expiry():
    limiter = HourlyRateLimiter(limit=2, window_seconds=10)
    user = UserId("u1")
    now = datetime.now()

    with patch("src.services.rate_limit.time") as mock_time:
        mock_time.monotonic.return_value = 0.0
        assert limiter.allow(user, now) is True
        assert limiter.allow(user, now) is True
        assert limiter.allow(user, now) is False   # at limit

        mock_time.monotonic.return_value = 11.0    # window expired
        assert limiter.allow(user, now) is True    # allowed again
```

**Step 4: Run full suite**

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/services/rate_limit.py tests/test_rate_limit.py
git commit -m "fix: rate limiter uses time.monotonic() to avoid wall-clock skew"
```

---

## Task 5: Admin API — fix XSS and constant-time token comparison

**Why:** `settings.analysis_prompt` rendered raw into HTML allows stored XSS. Token comparison via `!=` is timing-oracle.

**Files:**
- Modify: `src/app/admin_api.py`
- No new tests needed (visual/security fix; existing tests cover routes)

**Step 1: Add `html.escape()` to every field interpolated into HTML**

In `admin_api.py`, import `html` and wrap every `{settings.X}` that lands in an HTML attribute or text node:

```python
# src/app/admin_api.py — add at top
import html as _html
```

Replace raw interpolations with escaped versions. Key spots:

```python
# Line ~113 — users list (telegram_id comes from DB, could be injected):
users_html = "".join(f"<li>{_html.escape(user)}</li>" for user in users) or "<li>Нет пользователей</li>"

# Line ~249 — hidden token field:
<input type="hidden" name="token" value="{_html.escape(token or '')}" />

# Line ~251 — input values:
<input name="max_cases" value="{_html.escape(str(settings.max_cases))}" />
<input name="max_documents_per_case" value="{_html.escape(str(settings.max_documents_per_case))}" />
# ... repeat for ALL numeric/string settings fields ...

# Line ~270 — checkboxes (boolean, just safe as-is, no escaping needed)

# Line ~280 — THE CRITICAL ONE (stored XSS):
<textarea name="analysis_prompt">{_html.escape(settings.analysis_prompt)}</textarea>
```

**Step 2: Fix token comparison to constant-time**

```python
# src/app/admin_api.py
import hmac

# Replace in require_auth():
if not hmac.compare_digest(provided, token):
    raise HTTPException(status_code=403, detail="Forbidden")

# Replace in require_ui_token():
if not hmac.compare_digest(token, config.admin_auth_token):
    raise HTTPException(status_code=403, detail="Forbidden")
```

**Step 3: Run tests**

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 4: Commit**

```bash
git add src/app/admin_api.py
git commit -m "fix: admin UI XSS (html.escape all interpolations) + constant-time token comparison"
```

---

## Task 6: Admin API — replace `?token=` URL param with HTTP-only session cookie

**Why:** The admin token currently appears in every URL (`/admin?token=...`), server access logs, browser history, and HTTP Referer headers.

**Files:**
- Modify: `src/app/admin_api.py`

**Step 1: Add session management and login page to `admin_api.py`**

```python
# src/app/admin_api.py — additions at the top (after existing imports)
import secrets
import time as _time
from fastapi import Cookie, Response as FResponse
from fastapi.responses import HTMLResponse, RedirectResponse

# In-memory session store (sufficient for a single admin user)
_sessions: dict[str, float] = {}   # session_id → created_at (monotonic)
_SESSION_TTL = 8 * 3600             # 8 hours


def _check_session(session_id: str | None) -> bool:
    if not session_id or session_id not in _sessions:
        return False
    if _time.monotonic() - _sessions[session_id] > _SESSION_TTL:
        del _sessions[session_id]
        return False
    return True


def require_ui_session(session_id: str | None = Cookie(default=None)) -> None:
    if not _check_session(session_id):
        raise HTTPException(
            status_code=303,
            headers={"Location": "/admin/login"},
        )
```

**Step 2: Add login GET (form) and POST (authenticate) endpoints**

```python
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page() -> HTMLResponse:
    return HTMLResponse("""
    <html><head><title>KAD Bot Login</title>
    <style>
      body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f4f7fb}
      .card{background:#fff;padding:32px;border-radius:14px;box-shadow:0 12px 28px rgba(0,0,0,.1);width:320px}
      h2{margin:0 0 20px;font-size:22px}
      input{width:100%;box-sizing:border-box;border:1px solid #d8e1ef;border-radius:8px;padding:10px;font-size:14px;margin-bottom:12px}
      button{width:100%;background:#0f5fff;color:#fff;border:0;border-radius:8px;padding:11px;font-size:14px;font-weight:600;cursor:pointer}
      .err{color:#b42318;font-size:13px;margin-bottom:10px}
    </style></head>
    <body><div class="card">
      <h2>Вход в панель управления</h2>
      <form method="post" action="/admin/login">
        <input type="password" name="token" placeholder="Admin Token" autofocus />
        <button type="submit">Войти</button>
      </form>
    </div></body></html>
    """)


@app.post("/admin/login")
def admin_login(
    token: str = Form(...),
    response: FResponse = None,  # type: ignore[assignment]
) -> RedirectResponse:
    stored = config.admin_auth_token or ""
    if not stored or not hmac.compare_digest(token, stored):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login?error=1"})
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = _time.monotonic()
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        "session_id",
        session_id,
        httponly=True,
        samesite="strict",
        max_age=int(_SESSION_TTL),
    )
    return resp


@app.post("/admin/logout")
def admin_logout() -> RedirectResponse:
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie("session_id")
    return resp
```

**Step 3: Update `admin_ui` and all POST handlers — remove `?token=` pattern**

```python
# Before:
def admin_ui(token: str | None = None) -> HTMLResponse:
    require_ui_token(token)

# After:
def admin_ui(session_id: str | None = Cookie(default=None)) -> HTMLResponse:
    require_ui_session(session_id)
    # Remove all <input type="hidden" name="token"> from the HTML
    # Remove token from all form action URLs
    # Add logout button somewhere in the UI
```

Update all three POST handlers (`/admin/settings`, `/admin/users/grant`, `/admin/users/revoke`):
```python
# Remove: token: str = Form(...)  and  require_ui_token(token)
# Add:    session_id: str | None = Cookie(default=None)  and  require_ui_session(session_id)
# Change: return RedirectResponse(url=f"/admin?token={token}", ...)
# To:     return RedirectResponse(url="/admin", ...)
```

**Step 4: Add a logout button to the admin HTML**

In the `admin_ui` HTML, add near the title:
```html
<form method="post" action="/admin/logout" style="float:right">
  <button class="btn" style="background:#5b6b84">Выйти</button>
</form>
```

**Step 5: Run tests**

```bash
uv run pytest tests/ -q
```
Expected: all pass. (Admin API tests that use Bearer auth against JSON endpoints are unaffected.)

**Step 6: Commit**

```bash
git add src/app/admin_api.py
git commit -m "feat: admin UI session cookie auth — token no longer leaks into URLs/logs"
```

---

## Task 7: Admin API — add rate limiting to prevent brute-force

**Why:** The admin API has no rate limiting. Any attacker who finds the URL can attempt unlimited token guesses per second.

**Files:**
- Modify: `src/app/admin_api.py`

**Step 1: Add per-IP rate limiter for admin login**

```python
# src/app/admin_api.py — add near the top (after session vars)
from collections import defaultdict

_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_RATE_LIMIT = 5       # max attempts
_LOGIN_RATE_WINDOW = 60.0   # per 60 seconds


def _check_login_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = _time.monotonic()
    attempts = _login_attempts[ip]
    # Remove old attempts outside window
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_RATE_WINDOW]
    if len(_login_attempts[ip]) >= _LOGIN_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    _login_attempts[ip].append(now)
```

**Step 2: Call `_check_login_rate` in `admin_login` POST**

```python
@app.post("/admin/login")
def admin_login(request: Request, token: str = Form(...)) -> RedirectResponse:
    _check_login_rate(request)
    stored = config.admin_auth_token or ""
    if not stored or not hmac.compare_digest(token, stored):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login?error=1"})
    ...
```

Also apply to the Bearer token API endpoints via a middleware or dependency — add to `require_auth`:

```python
# Note: Bearer auth endpoints are typically called by scripts/tools, not browsers,
# so a simple per-IP sliding window in require_auth is sufficient.
# Add the same check there (or trust network-level firewall for the JSON API).
```

**Step 3: Run tests**

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 4: Commit**

```bash
git add src/app/admin_api.py
git commit -m "fix: admin login rate limiting (5 attempts/60s per IP)"
```

---

## Task 8: Active requests — persist to SQLite so admin API can see them

**Why:** `/requests/active` in the admin API always returns `[]` because bot and admin run as separate OS processes with separate in-memory registries.

**Approach:** Add a lightweight `active_requests` table. Bot writes on `start()`, `set_phase()`, and `finish()`. Admin reads from DB. Counter updates (attempted/successful) stay in-memory for performance — they change too rapidly for DB writes.

**Files:**
- Modify: `src/infrastructure/sqlite.py` (add table to schema)
- Create: `src/infrastructure/active_requests_repository.py`
- Modify: `src/services/active_requests.py` (inject optional repo)
- Modify: `src/app/container.py` (wire repo into registry)
- Modify: `src/app/admin_api.py` (read from DB instead of registry)
- Create: `tests/test_active_requests_repository.py`

**Step 1: Add table to SQLite schema in `sqlite.py`**

```python
# In _ensure_schema, add to the executescript:
create table if not exists active_requests (
    user_id text primary key,
    query_text text not null,
    phase text not null default 'counting',
    total_cases integer not null default 0,
    started_at text not null,
    updated_at text not null
);
```

**Step 2: Create `src/infrastructure/active_requests_repository.py`**

```python
from __future__ import annotations

from datetime import datetime

from src.infrastructure.sqlite import SqliteConnection


class ActiveRequestsRepository:
    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection

    def upsert(self, user_id: str, query_text: str, phase: str, total_cases: int) -> None:
        now = datetime.now().isoformat()
        with self._connection.connect() as conn:
            conn.execute(
                """
                insert into active_requests (user_id, query_text, phase, total_cases, started_at, updated_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(user_id) do update set
                    query_text=excluded.query_text,
                    phase=excluded.phase,
                    total_cases=excluded.total_cases,
                    updated_at=excluded.updated_at
                """,
                (user_id, query_text, phase, total_cases, now, now),
            )

    def update_phase(self, user_id: str, phase: str) -> None:
        now = datetime.now().isoformat()
        with self._connection.connect() as conn:
            conn.execute(
                "update active_requests set phase=?, updated_at=? where user_id=?",
                (phase, now, user_id),
            )

    def delete(self, user_id: str) -> None:
        with self._connection.connect() as conn:
            conn.execute("delete from active_requests where user_id=?", (user_id,))

    def list_all(self) -> list[dict]:
        with self._connection.connect() as conn:
            rows = conn.execute(
                "select user_id, query_text, phase, total_cases, started_at from active_requests"
            ).fetchall()
        return [dict(row) for row in rows]
```

**Step 3: Write tests for the repository**

Create `tests/test_active_requests_repository.py`:

```python
from __future__ import annotations

import pytest
from src.infrastructure.active_requests_repository import ActiveRequestsRepository
from src.infrastructure.sqlite import SqliteConnection


@pytest.fixture
def repo(tmp_path):
    conn = SqliteConnection(str(tmp_path / "test.db"))
    return ActiveRequestsRepository(conn)


def test_upsert_and_list(repo):
    repo.upsert("u1", "query text", "counting", 0)
    rows = repo.list_all()
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["phase"] == "counting"


def test_update_phase(repo):
    repo.upsert("u1", "q", "counting", 0)
    repo.update_phase("u1", "analyzing")
    rows = repo.list_all()
    assert rows[0]["phase"] == "analyzing"


def test_delete_removes_entry(repo):
    repo.upsert("u1", "q", "counting", 0)
    repo.delete("u1")
    assert repo.list_all() == []


def test_upsert_is_idempotent(repo):
    repo.upsert("u1", "q", "counting", 0)
    repo.upsert("u1", "q", "analyzing", 50)
    rows = repo.list_all()
    assert len(rows) == 1
    assert rows[0]["phase"] == "analyzing"
    assert rows[0]["total_cases"] == 50
```

**Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_active_requests_repository.py -v
```
Expected: 4 passed.

**Step 5: Inject optional repo into `ActiveRequestRegistry`**

```python
# src/services/active_requests.py — update __init__ and 3 methods

from src.infrastructure.active_requests_repository import ActiveRequestsRepository

class ActiveRequestRegistry:
    def __init__(self, db_repo: ActiveRequestsRepository | None = None) -> None:
        self._lock = threading.Lock()
        self._active: dict[UserId, ActiveRequest] = {}
        self._db = db_repo   # optional — None in tests that don't need persistence

    def start(self, user_id: UserId, query_text: str, total_cases: int, phase: str = "counting") -> bool:
        with self._lock:
            if user_id in self._active:
                return False
            self._active[user_id] = ActiveRequest(
                user_id=user_id, query_text=query_text, total_cases=total_cases, phase=phase,
            )
        if self._db:
            try:
                self._db.upsert(user_id.value, query_text, phase, total_cases)
            except Exception:
                pass   # DB failure must never break the bot
        return True

    def set_phase(self, user_id: UserId, phase: str) -> None:
        with self._lock:
            active = self._active.get(user_id)
            if active is None:
                return
            active.phase = phase
        if self._db:
            try:
                self._db.update_phase(user_id.value, phase)
            except Exception:
                pass

    def finish(self, user_id: UserId) -> None:
        with self._lock:
            self._active.pop(user_id, None)
        if self._db:
            try:
                self._db.delete(user_id.value)
            except Exception:
                pass
```

**Step 6: Wire repo in `container.py`**

```python
# src/app/container.py — add to __init__:
from src.infrastructure.active_requests_repository import ActiveRequestsRepository
...
self.active_requests_repository = ActiveRequestsRepository(self.connection)
self.active_requests = ActiveRequestRegistry(db_repo=self.active_requests_repository)
```

**Step 7: Update admin API `/requests/active` to read from DB**

```python
# src/app/admin_api.py
@app.get("/requests/active", dependencies=[Depends(require_auth)])
def active_requests_endpoint() -> dict:
    rows = container.active_requests_repository.list_all()
    return {
        "requests": [
            {
                "telegram_id": row["user_id"],
                "phase": row["phase"],
                "total_cases": row["total_cases"],
                "query_text": row["query_text"],
                "started_at": row["started_at"],
                # In-memory counters not available cross-process (by design)
                "collected_cases": 0,
                "processed_cases": 0,
                "attempted_cases": 0,
                "successful_cases": 0,
                "retry_count": 0,
            }
            for row in rows
        ]
    }
```

**Step 8: Run full test suite**

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 9: Commit**

```bash
git add src/infrastructure/active_requests_repository.py \
        src/infrastructure/sqlite.py \
        src/services/active_requests.py \
        src/app/container.py \
        src/app/admin_api.py \
        tests/test_active_requests_repository.py
git commit -m "feat: persist active requests to SQLite so admin API sees bot requests cross-process"
```

---

## Task 9: Better time estimate for large queries

**Why:** `estimate_minutes()` returns 5 for anything above 300 cases, even 500 cases. Users get the same estimate for vastly different query sizes.

**Files:**
- Modify: `src/core/estimation.py`
- Modify: `tests/test_estimation.py`

**Step 1: Update tests first**

```python
# tests/test_estimation.py — update or add:
def test_estimate_scales_above_300():
    assert estimate_minutes(300) == 5
    assert estimate_minutes(400) == 7
    assert estimate_minutes(500) == 9
```

**Step 2: Update `estimation.py`**

```python
def estimate_minutes(case_count: int) -> int:
    if case_count <= 0:
        return 0
    if case_count <= 50:
        return 1
    if case_count <= 100:
        return 2
    if case_count <= 200:
        return 3
    if case_count <= 300:
        return 5
    # ~1 minute per 55 additional cases above 300
    extra = case_count - 300
    return 5 + round(extra / 55)
```

**Step 3: Run tests**

```bash
uv run pytest tests/test_estimation.py -v
```
Expected: all pass.

**Step 4: Run full suite**

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/core/estimation.py tests/test_estimation.py
git commit -m "fix: estimate_minutes scales linearly above 300 cases instead of capping at 5 min"
```

---

## Task 10: Final verification

**Step 1: Run the complete test suite one final time**

```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -20
```
Expected: all tests pass (≥152).

**Step 2: Verify no import errors across the codebase**

```bash
uv run python -c "from src.app.container import Container; from src.app.config import load_config; print('OK')"
```
Expected: `OK`

**Step 3: Spot-check the admin HTML for unescaped interpolations**

```bash
uv run python -c "
from src.app.admin_api import admin_ui
from fastapi import Cookie
# Craft a malicious analysis_prompt
from src.infrastructure.sqlite import SqliteConnection
import tempfile, os
" 2>&1 || echo "manual check needed"
```

Manual check: grep for any `{settings.` in `admin_api.py` that is NOT wrapped in `_html.escape(...)`:
```bash
grep -n '{settings\.' src/app/admin_api.py | grep -v '_html.escape'
```
Expected: no output.

**Step 4: Final commit message summary**

```bash
git log --oneline -10
```

Should show commits for each task above.

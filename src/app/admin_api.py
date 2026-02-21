from __future__ import annotations

import hmac
import html as _html
import logging
import secrets
import time

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from src.app.bot_logging import (
    clear_request_context,
    configure_logging,
    log_event,
    new_request_id,
    set_request_context,
)
from src.app.config import load_config
from src.app.container import Container

configure_logging("admin-api")
logger = logging.getLogger("admin_api.routes")
db_logger = logging.getLogger("admin_api.db")
auth_logger = logging.getLogger("admin_api.auth")

app = FastAPI(title="KAD Bot Admin API")

config = load_config()
container = Container(config)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


_SESSION_TTL = 8 * 3600  # 8 hours
_LOGIN_RATE_LIMIT = 5  # max attempts
_LOGIN_RATE_WINDOW = 60  # seconds


def _create_session() -> str:
    sid = secrets.token_urlsafe(32)
    container.session_repository.create_session(sid, _SESSION_TTL)
    return sid


def _validate_session(sid: str | None) -> bool:
    if not sid:
        return False
    return container.session_repository.validate_session(sid)


def _check_login_rate_limit(ip: str) -> bool:
    """Returns True if allowed, False if rate limited."""
    attempts = container.rate_limit_repository.count_attempts(ip, _LOGIN_RATE_WINDOW)
    if attempts >= _LOGIN_RATE_LIMIT:
        return False
    container.rate_limit_repository.add_attempt(ip)
    return True


class SettingsModel(BaseModel):
    max_cases: int
    max_documents_per_case: int
    max_pages: int
    fetch_concurrency_min: int
    fetch_concurrency_max: int
    slow_alert_minutes: int
    details_cache_ttl_seconds: int
    allow_all_users: bool
    unknown_outcome_threshold_percent: int
    court_mismatch_threshold_percent: int
    min_known_outcomes: int
    send_partial_file_on_quality_fail: bool
    analysis_prompt: str


class AllowedUserModel(BaseModel):
    telegram_id: str


class ActiveRequestModel(BaseModel):
    telegram_id: str
    phase: str
    total_cases: int
    collected_cases: int
    processed_cases: int
    attempted_cases: int
    successful_cases: int
    retry_count: int


def require_auth(
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> None:
    token = config.admin_auth_token
    if not token:
        raise HTTPException(
            status_code=500, detail="ADMIN_AUTH_TOKEN is not configured"
        )
    if x_admin_token:
        provided = x_admin_token
    elif authorization:
        if authorization.startswith("Bearer "):
            provided = authorization.replace("Bearer ", "", 1)
        else:
            provided = authorization
    if not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=403, detail="Forbidden")


async def require_admin_session(request: Request) -> None:
    session_id = request.cookies.get("admin_session")
    if not _validate_session(session_id):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("X-CSRF-Token")

        # Check header first (AJAX), then try form data (standard submission)
        csrf_token = csrf_header
        if (
            not csrf_token
            and "application/x-www-form-urlencoded"
            in request.headers.get("Content-Type", "")
        ):
            form_data = await request.form()
            csrf_token = form_data.get("csrf_token")

        if not csrf_cookie or csrf_cookie != csrf_token:
            raise HTTPException(status_code=403, detail="CSRF token mismatch")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = new_request_id()
    set_request_context(request_id, None, "admin_api")
    start = time.monotonic()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        log_event(
            logger,
            "request.completed",
            method=request.method,
            path=str(request.url.path),
            status=getattr(response, "status_code", 500),
            duration_ms=duration_ms,
        )
        clear_request_context()


@app.get("/health")
def health() -> dict:
    # 1. Check Database (read check)
    db_status = "error"
    try:
        with container.connection.get_connection() as conn:
            conn.execute("SELECT 1")
            db_status = "ok"
    except Exception as e:
        log_event("health.db_error", error=str(e))

    # 2. Check Critical Config
    config_status = "ok"
    missing_keys = []
    if (
        not container.config.kad_api_key
        or "your_key_here" in container.config.kad_api_key
    ):
        missing_keys.append("KAD_API_KEY")
    if not container.config.bot_token:
        missing_keys.append("TELEGRAM_BOT_TOKEN")

    if missing_keys:
        config_status = "missing_keys"

    # 3. Simple Metrics
    active_count = len(container.active_requests.list_all())

    overall_status = "ok" if db_status == "ok" and config_status == "ok" else "degraded"

    return {
        "status": overall_status,
        "database": db_status,
        "config": {
            "status": config_status,
            "missing_keys": missing_keys,
            "llm_enabled": bool(container.config.llm_api_key),
        },
        "metrics": {"active_requests": active_count},
    }


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse("""
<!DOCTYPE html><html><head><title>Admin Login</title></head>
<body style="font-family:sans-serif;max-width:400px;margin:100px auto;padding:20px">
<h2>Admin Login</h2>
<form method="post" action="/admin/login">
<input type="password" name="token" placeholder="Admin token" style="width:100%;padding:8px;margin:8px 0" required>
<button type="submit" style="width:100%;padding:8px;background:#007bff;color:white;border:none;cursor:pointer">Login</button>
</form></body></html>
""")


@app.post("/admin/login")
async def login(request: Request, token: str = Form(...)):
    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(client_ip):
        return HTMLResponse(
            "Too many login attempts. Try again later.", status_code=429
        )
    if not config.admin_auth_token:
        return HTMLResponse("Admin token is not configured.", status_code=500)
    if not hmac.compare_digest(token, config.admin_auth_token):
        auth_logger.warning("admin.login_failed", extra={"data": {"ip": client_ip}})
        return HTMLResponse("Invalid token", status_code=401)
    sid = _create_session()
    response = RedirectResponse(url="/admin/", status_code=303)
    response.set_cookie(
        "admin_session",
        sid,
        httponly=True,
        samesite="strict",
        secure=True,
        max_age=_SESSION_TTL,
    )
    # Set CSRF token on login
    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return response


@app.get("/admin/logout")
async def logout(request: Request):
    sid = request.cookies.get("admin_session")
    if sid:
        container.session_repository.delete_session(sid)
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    response.delete_cookie("csrf_token")  # Delete CSRF token on logout
    return response


@app.get(
    "/admin/",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_session)],
)
@app.get(
    "/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin_session)]
)
def admin_ui(request: Request) -> HTMLResponse:
    if not _validate_session(request.cookies.get("admin_session")):
        return RedirectResponse(url="/admin/login", status_code=303)
    settings = container.settings_service.get_settings()
    users = container.allowed_users_repository.list_all()
    users_html = (
        "".join(f"<li>{_html.escape(user)}</li>" for user in users)
        or "<li>Нет пользователей</li>"
    )
    csrf_token = request.cookies.get("csrf_token", "")
    csrf_input = f'<input type="hidden" name="csrf_token" value="{csrf_token}" />'
    html = f"""
    <html>
      <head>
        <title>KAD Bot Admin</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root {{
            --bg: #f4f7fb;
            --card: #ffffff;
            --line: #d8e1ef;
            --text: #1a2433;
            --muted: #5b6b84;
            --accent: #0f5fff;
            --accent-hover: #0d4ed1;
            --danger: #b42318;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: "Segoe UI", "Helvetica Neue", sans-serif;
            background: radial-gradient(circle at top right, #dbeafe 0%, var(--bg) 45%);
            color: var(--text);
          }}
          .wrap {{
            max-width: 980px;
            margin: 28px auto;
            padding: 0 16px;
          }}
          .title {{
            margin: 0 0 14px;
            font-size: 34px;
            font-weight: 800;
            letter-spacing: 0.2px;
          }}
          .grid {{
            display: grid;
            grid-template-columns: 1.5fr 1fr;
            gap: 16px;
          }}
          .card {{
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 18px;
            box-shadow: 0 12px 28px rgba(15, 23, 42, 0.06);
          }}
          .card h2 {{
            margin: 0 0 14px;
            font-size: 20px;
            font-weight: 700;
          }}
          .row {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 10px;
          }}
          label {{
            display: block;
            color: var(--muted);
            font-size: 13px;
            margin-bottom: 6px;
          }}
          input, textarea {{
            width: 100%;
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 10px 11px;
            font-size: 14px;
            color: var(--text);
            background: #fff;
          }}
          textarea {{
            resize: vertical;
            min-height: 92px;
          }}
          .checkbox {{
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 6px 0 2px;
          }}
          .checkbox input {{
            width: 16px;
            height: 16px;
            margin: 0;
          }}
          .checkbox label {{
            margin: 0;
            color: var(--text);
            font-size: 14px;
          }}
          .btn {{
            border: 0;
            border-radius: 10px;
            padding: 10px 14px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            color: #fff;
            background: var(--accent);
          }}
          .btn:hover {{ background: var(--accent-hover); }}
          .btn-danger {{ background: var(--danger); }}
          .btn-danger:hover {{ background: #8f1912; }}
          ul {{
            margin: 0 0 12px;
            padding-left: 22px;
          }}
          li {{ margin: 6px 0; }}
          .stack {{ display: grid; gap: 10px; }}
          .inline-form {{
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 8px;
            align-items: end;
          }}
          .hint {{
            color: var(--muted);
            font-size: 12px;
            margin-top: 6px;
          }}
          @media (max-width: 900px) {{
            .grid {{ grid-template-columns: 1fr; }}
            .row {{ grid-template-columns: 1fr; }}
          }}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1 class="title">Панель управления ботом КАД</h1>
          <div style="text-align:right;margin-bottom:8px"><a href="/admin/logout">Выйти</a></div>
          <div class="grid">
            <section class="card">
              <h2>Основные настройки</h2>
              <form method="post" action="/admin/settings" class="stack">
                {csrf_input}
                <div class="row">
                  <div><label>Максимум дел в одном анализе</label><input name="max_cases" value="{_html.escape(str(settings.max_cases))}" /></div>
                  <div><label>Сколько документов учитывать по каждому делу</label><input name="max_documents_per_case" value="{_html.escape(str(settings.max_documents_per_case))}" /></div>
                </div>
                <div class="row">
                  <div><label>Сколько страниц результатов проверять</label><input name="max_pages" value="{_html.escape(str(settings.max_pages))}" /></div>
                  <div><label>Через сколько минут показать сообщение «анализ еще идет»</label><input name="slow_alert_minutes" value="{_html.escape(str(settings.slow_alert_minutes))}" /></div>
                </div>
                <div class="row">
                  <div><label>Минимальная скорость загрузки данных (тех. параметр)</label><input name="fetch_concurrency_min" value="{_html.escape(str(settings.fetch_concurrency_min))}" /></div>
                  <div><label>Максимальная скорость загрузки данных (тех. параметр)</label><input name="fetch_concurrency_max" value="{_html.escape(str(settings.fetch_concurrency_max))}" /></div>
                </div>
                <div><label>Срок хранения кэша, в секундах (например 86400 = 24 часа)</label><input name="details_cache_ttl_seconds" value="{_html.escape(str(settings.details_cache_ttl_seconds))}" /></div>
                <div class="row">
                  <div><label>Порог неизвестных исходов, % (при превышении анализ останавливается)</label><input name="unknown_outcome_threshold_percent" value="{_html.escape(str(settings.unknown_outcome_threshold_percent))}" /></div>
                  <div><label>Порог несовпадения суда, % (при превышении анализ останавливается)</label><input name="court_mismatch_threshold_percent" value="{_html.escape(str(settings.court_mismatch_threshold_percent))}" /></div>
                </div>
                <div class="row">
                  <div><label>Минимум дел с определенным исходом для итоговой сводки</label><input name="min_known_outcomes" value="{_html.escape(str(settings.min_known_outcomes))}" /></div>
                  <div class="checkbox" style="align-items: center;">
                    <input id="send_partial_file_on_quality_fail" type="checkbox" name="send_partial_file_on_quality_fail" value="true" {"checked" if settings.send_partial_file_on_quality_fail else ""} />
                    <label for="send_partial_file_on_quality_fail">Отправлять частичную выгрузку при низком качестве данных</label>
                  </div>
                </div>
                <div class="checkbox">
                  <input id="allow_all_users" type="checkbox" name="allow_all_users" value="true" {"checked" if settings.allow_all_users else ""} />
                  <label for="allow_all_users">Разрешить пользоваться ботом всем (без списка разрешенных пользователей)</label>
                </div>
                <div>
                  <label>Текст инструкции для анализа (внутренний промпт)</label>
                  <textarea name="analysis_prompt">{_html.escape(str(settings.analysis_prompt))}</textarea>
                  <div class="hint">Этот текст используется ботом при формировании итоговой сводки.</div>
                </div>
                <button class="btn" type="submit">Сохранить настройки</button>
              </form>
            </section>
            <section class="card">
              <h2>Доступ к боту</h2>
              <ul>{users_html}</ul>
              <form method="post" action="/admin/users/grant" class="stack">
                {csrf_input}
                <div class="inline-form">
                  <div><label>ID пользователя в Telegram</label><input name="telegram_id" /></div>
                  <button class="btn" type="submit">Выдать доступ</button>
                </div>
              </form>
              <form method="post" action="/admin/users/revoke" class="stack" style="margin-top: 10px;">
                {csrf_input}
                <div class="inline-form">
                  <div><label>ID пользователя в Telegram</label><input name="telegram_id" /></div>
                  <button class="btn btn-danger" type="submit">Забрать доступ</button>
                </div>
              </form>
            </section>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.post("/admin/settings", dependencies=[Depends(require_admin_session)])
def admin_update_settings(
    request: Request,
    max_cases: int = Form(...),
    max_documents_per_case: int = Form(...),
    max_pages: int = Form(...),
    fetch_concurrency_min: int = Form(...),
    fetch_concurrency_max: int = Form(...),
    slow_alert_minutes: int = Form(...),
    details_cache_ttl_seconds: int = Form(...),
    allow_all_users: bool = Form(False),
    unknown_outcome_threshold_percent: int = Form(...),
    court_mismatch_threshold_percent: int = Form(...),
    min_known_outcomes: int = Form(...),
    send_partial_file_on_quality_fail: bool = Form(False),
    analysis_prompt: str = Form(...),
) -> RedirectResponse:
    try:
        container.settings_service.update_settings(
            max_cases=max_cases,
            max_documents_per_case=max_documents_per_case,
            max_pages=max_pages,
            fetch_concurrency_min=fetch_concurrency_min,
            fetch_concurrency_max=fetch_concurrency_max,
            slow_alert_minutes=slow_alert_minutes,
            details_cache_ttl_seconds=details_cache_ttl_seconds,
            allow_all_users=allow_all_users,
            unknown_outcome_threshold_percent=unknown_outcome_threshold_percent,
            court_mismatch_threshold_percent=court_mismatch_threshold_percent,
            min_known_outcomes=min_known_outcomes,
            send_partial_file_on_quality_fail=send_partial_file_on_quality_fail,
            analysis_prompt=analysis_prompt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/admin/", status_code=303)


@app.post("/admin/users/grant", dependencies=[Depends(require_admin_session)])
def admin_grant_user(
    request: Request, telegram_id: str = Form(...)
) -> RedirectResponse:
    container.allowed_users_repository.grant(telegram_id)
    return RedirectResponse(url="/admin/", status_code=303)


@app.post("/admin/users/revoke", dependencies=[Depends(require_admin_session)])
def admin_revoke_user(
    request: Request, telegram_id: str = Form(...)
) -> RedirectResponse:
    container.allowed_users_repository.revoke(telegram_id)
    return RedirectResponse(url="/admin/", status_code=303)


@app.get("/settings", dependencies=[Depends(require_auth)])
def get_settings() -> SettingsModel:
    settings = container.settings_service.get_settings()
    return SettingsModel(
        max_cases=settings.max_cases,
        max_documents_per_case=settings.max_documents_per_case,
        max_pages=settings.max_pages,
        fetch_concurrency_min=settings.fetch_concurrency_min,
        fetch_concurrency_max=settings.fetch_concurrency_max,
        slow_alert_minutes=settings.slow_alert_minutes,
        details_cache_ttl_seconds=settings.details_cache_ttl_seconds,
        allow_all_users=settings.allow_all_users,
        unknown_outcome_threshold_percent=settings.unknown_outcome_threshold_percent,
        court_mismatch_threshold_percent=settings.court_mismatch_threshold_percent,
        min_known_outcomes=settings.min_known_outcomes,
        send_partial_file_on_quality_fail=settings.send_partial_file_on_quality_fail,
        analysis_prompt=settings.analysis_prompt,
    )


@app.put("/settings", dependencies=[Depends(require_auth)])
def update_settings(payload: SettingsModel) -> SettingsModel:
    try:
        settings = container.settings_service.update_settings(
            max_cases=payload.max_cases,
            max_documents_per_case=payload.max_documents_per_case,
            max_pages=payload.max_pages,
            fetch_concurrency_min=payload.fetch_concurrency_min,
            fetch_concurrency_max=payload.fetch_concurrency_max,
            slow_alert_minutes=payload.slow_alert_minutes,
            details_cache_ttl_seconds=payload.details_cache_ttl_seconds,
            allow_all_users=payload.allow_all_users,
            unknown_outcome_threshold_percent=payload.unknown_outcome_threshold_percent,
            court_mismatch_threshold_percent=payload.court_mismatch_threshold_percent,
            min_known_outcomes=payload.min_known_outcomes,
            send_partial_file_on_quality_fail=payload.send_partial_file_on_quality_fail,
            analysis_prompt=payload.analysis_prompt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SettingsModel(
        max_cases=settings.max_cases,
        max_documents_per_case=settings.max_documents_per_case,
        max_pages=settings.max_pages,
        fetch_concurrency_min=settings.fetch_concurrency_min,
        fetch_concurrency_max=settings.fetch_concurrency_max,
        slow_alert_minutes=settings.slow_alert_minutes,
        details_cache_ttl_seconds=settings.details_cache_ttl_seconds,
        allow_all_users=settings.allow_all_users,
        unknown_outcome_threshold_percent=settings.unknown_outcome_threshold_percent,
        court_mismatch_threshold_percent=settings.court_mismatch_threshold_percent,
        min_known_outcomes=settings.min_known_outcomes,
        send_partial_file_on_quality_fail=settings.send_partial_file_on_quality_fail,
        analysis_prompt=settings.analysis_prompt,
    )


@app.get("/access/users", dependencies=[Depends(require_auth)])
def list_users() -> dict:
    users = container.allowed_users_repository.list_all()
    return {"users": [{"telegram_id": user_id} for user_id in users]}


@app.post("/access/users", dependencies=[Depends(require_auth)], status_code=201)
def grant_access(payload: AllowedUserModel) -> AllowedUserModel:
    container.allowed_users_repository.grant(payload.telegram_id)
    return payload


@app.delete(
    "/access/users",
    dependencies=[Depends(require_auth)],
    status_code=204,
    response_class=Response,
    response_model=None,
)
async def revoke_access(payload: AllowedUserModel) -> None:
    container.allowed_users_repository.revoke(payload.telegram_id)


@app.get("/requests/active", dependencies=[Depends(require_auth)])
def active_requests() -> dict:
    requests = container.active_requests.list_all()
    return {
        "requests": [
            ActiveRequestModel(
                telegram_id=request.user_id.value,
                phase=request.phase,
                total_cases=request.total_cases,
                collected_cases=request.collected_cases,
                processed_cases=request.processed_cases,
                attempted_cases=request.attempted_cases,
                successful_cases=request.successful_cases,
                retry_count=request.retry_count,
            ).model_dump()
            for request in requests
        ]
    }

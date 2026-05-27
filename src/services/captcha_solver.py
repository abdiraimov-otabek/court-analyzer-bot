from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import httpx

from src.app.bot_logging import log_event


class CaptchaExtractionError(Exception):
    pass


class CaptchaSolvingError(Exception):
    pass


class CaptchaTimeoutError(Exception):
    pass


@dataclass(frozen=True)
class CaptchaChallenge:
    captcha_type: str
    sitekey: str
    page_url: str
    action: str | None = None


@dataclass(frozen=True)
class CaptchaSolution:
    token: str
    captcha_type: str
    elapsed_seconds: float


class _SitekeyExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sitekeys: list[str] = []
        self.captcha_types: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "div":
            class_val = attrs_dict.get("class", "") or ""
            data_sitekey = attrs_dict.get("data-sitekey")
            if data_sitekey:
                if "h-captcha" in class_val or "hcaptcha" in class_val:
                    self.sitekeys.append(data_sitekey)
                    self.captcha_types.append("hcaptcha")
                elif "g-recaptcha" in class_val or "recaptcha" in class_val:
                    self.sitekeys.append(data_sitekey)
                    self.captcha_types.append("recaptcha_v2")
                else:
                    self.sitekeys.append(data_sitekey)
                    self.captcha_types.append("recaptcha_v2")
        elif tag == "iframe":
            src = attrs_dict.get("src", "") or ""
            if "recaptcha" in src or "google.com/recaptcha" in src:
                match = re.search(r"[/?]k=([A-Za-z0-9_-]{20,})", src)
                if match:
                    self.sitekeys.append(match.group(1))
                    self.captcha_types.append("recaptcha_v2")
            elif "hcaptcha" in src:
                match = re.search(r"[/?]k=([A-Za-z0-9_-]{20,})", src)
                if match:
                    self.sitekeys.append(match.group(1))
                    self.captcha_types.append("hcaptcha")


def extract_captcha_challenge(
    html_content: str, page_url: str
) -> CaptchaChallenge | None:
    normalized = html_content.lower()

    has_recaptcha = bool(
        re.search(r"recaptcha", normalized)
        or re.search(r"g-recaptcha", normalized)
    )
    has_hcaptcha = bool(re.search(r"h-?captcha", normalized))
    has_pravocaptcha = bool(re.search(r"pravocaptcha", normalized))

    if not (has_recaptcha or has_hcaptcha or has_pravocaptcha):
        return None

    if has_pravocaptcha:
        sitekey = _extract_pravocaptcha_sitekey(html_content)
        if sitekey:
            return CaptchaChallenge(
                captcha_type="pravocaptcha",
                sitekey=sitekey,
                page_url=page_url,
            )
        return CaptchaChallenge(
            captcha_type="pravocaptcha",
            sitekey="pravocaptcha",
            page_url=page_url,
        )

    extractor = _SitekeyExtractor()
    try:
        extractor.feed(html_content)
    except Exception:
        pass

    if extractor.sitekeys:
        idx = 0
        captcha_type = extractor.captcha_types[0] if extractor.captcha_types else "recaptcha_v2"
        return CaptchaChallenge(
            captcha_type=captcha_type,
            sitekey=extractor.sitekeys[0],
            page_url=page_url,
        )

    sitekey = _extract_sitekey_regex(html_content)
    if sitekey:
        captcha_type = "hcaptcha" if has_hcaptcha else "recaptcha_v2"
        return CaptchaChallenge(
            captcha_type=captcha_type,
            sitekey=sitekey,
            page_url=page_url,
        )

    return None


def _extract_pravocaptcha_sitekey(html: str) -> str | None:
    patterns = [
        r'pravocaptcha["\s\.(,]*\s*["\']?([A-Za-z0-9_-]{10,})["\']?',
        r'data-sitekey=["\']([A-Za-z0-9_-]+)["\']',
        r'sitekey["\s:=]+["\']([A-Za-z0-9_-]+)["\']',
        r'captchaKey["\s:=]+["\']([A-Za-z0-9_-]+)["\']',
        r'public[_-]?key["\s:=]+["\']([A-Za-z0-9_-]+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            candidate = match.group(1)
            if len(candidate) >= 10:
                return candidate
    return None


def _extract_sitekey_regex(html: str) -> str | None:
    patterns = [
        r'data-sitekey=["\']([A-Za-z0-9_-]+)["\']',
        r'/k=([A-Za-z0-9_-]{20,})',
        r'sitekey["\s:=]+["\']([A-Za-z0-9_-]{20,})["\']',
        r'"sitekey"\s*:\s*"([A-Za-z0-9_-]+)"',
        r"grecaptcha\.render\([^,]+,\s*\{[^}]*['\"]sitekey['\"]\s*:\s*['\"]([A-Za-z0-9_-]+)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


class CaptchaSolver:
    TWO_CAPTCHA_BASE = "https://2captcha.com"

    def __init__(
        self,
        api_key: str,
        service_url: str | None = None,
        timeout_seconds: int = 180,
        poll_interval_seconds: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = (service_url or self.TWO_CAPTCHA_BASE).rstrip("/")
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._http_client = http_client
        self._owns_client = http_client is None
        self._logger = logging.getLogger("captcha_solver")

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30)
        return self._http_client

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        log_event(
            self._logger,
            "captcha.detected",
            captcha_type=challenge.captcha_type,
            sitekey=challenge.sitekey,
            page_url=challenge.page_url,
        )

        task_id = await self._submit(challenge)
        log_event(
            self._logger,
            "captcha.sent",
            task_id=task_id,
            captcha_type=challenge.captcha_type,
        )

        start = time.monotonic()
        token = await self._poll(task_id, start)
        elapsed = time.monotonic() - start

        log_event(
            self._logger,
            "captcha.solved",
            task_id=task_id,
            elapsed_seconds=round(elapsed, 1),
        )

        return CaptchaSolution(
            token=token,
            captcha_type=challenge.captcha_type,
            elapsed_seconds=elapsed,
        )

    async def _submit(self, challenge: CaptchaChallenge) -> str:
        client = await self._get_client()
        method = self._resolve_method(challenge.captcha_type)

        payload: dict[str, str] = {
            "key": self._api_key,
            "method": method,
            "googlekey": challenge.sitekey,
            "pageurl": challenge.page_url,
            "json": "1",
        }
        if challenge.action:
            payload["action"] = challenge.action

        response = await client.post(
            f"{self._base_url}/in.php",
            data=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") != 1:
            error_text = data.get("request", "unknown error")
            raise CaptchaSolvingError(f"CAPTCHA submission failed: {error_text}")

        return str(data["request"])

    async def _poll(self, task_id: str, start_time: float) -> str:
        client = await self._get_client()

        while True:
            elapsed = time.monotonic() - start_time
            if elapsed > self._timeout:
                raise CaptchaTimeoutError(
                    f"CAPTCHA solving timed out after {self._timeout}s"
                )

            await asyncio.sleep(self._poll_interval)

            response = await client.get(
                f"{self._base_url}/res.php",
                params={
                    "key": self._api_key,
                    "action": "get",
                    "id": task_id,
                    "json": "1",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == 1:
                return str(data["request"])

            error_text = data.get("request", "")
            if error_text == "CAPCHA_NOT_READY":
                continue

            if error_text == "ERROR_CAPTCHA_UNSOLVABLE":
                raise CaptchaSolvingError("CAPTCHA marked as unsolvable")

            if error_text.startswith("ERROR"):
                raise CaptchaSolvingError(f"CAPTCHA solving error: {error_text}")

            continue

    @staticmethod
    def _resolve_method(captcha_type: str) -> str:
        mapping = {
            "recaptcha_v2": "userrecaptcha",
            "hcaptcha": "hcaptcha",
            "pravocaptcha": "userrecaptcha",
        }
        return mapping.get(captcha_type, "userrecaptcha")


async def solve_and_retry_pdf(
    captcha_solver: CaptchaSolver,
    async_http_client: httpx.AsyncClient,
    target_url: str,
    html_content: str,
    timeout: float,
) -> bytes | None:
    challenge = extract_captcha_challenge(html_content, target_url)
    if challenge is None:
        return None

    try:
        solution = await captcha_solver.solve(challenge)
    except (CaptchaSolvingError, CaptchaTimeoutError) as exc:
        log_event(
            logging.getLogger("captcha_solver"),
            "captcha.failed",
            url=target_url,
            error=str(exc),
        )
        return None
    except Exception as exc:
        log_event(
            logging.getLogger("captcha_solver"),
            "captcha.failed",
            url=target_url,
            error=str(exc),
        )
        return None

    token_cookie_name = _resolve_cookie_name(challenge.captcha_type)

    retry_resp = await async_http_client.get(
        target_url,
        timeout=timeout,
        follow_redirects=True,
        cookies={token_cookie_name: solution.token},
    )

    if retry_resp.status_code == 200:
        ct = retry_resp.headers.get("content-type", "").lower()
        if "pdf" in ct or "octet-stream" in ct or not ct.startswith("text/html"):
            return retry_resp.content

    return None


def solve_and_retry_pdf_sync(
    captcha_solver_api_key: str,
    captcha_solver_url: str | None,
    sync_http_client: httpx.Client,
    target_url: str,
    html_content: str,
    timeout: float,
) -> bytes | None:
    challenge = extract_captcha_challenge(html_content, target_url)
    if challenge is None:
        return None

    log_event(
        logging.getLogger("captcha_solver"),
        "captcha.detected",
        captcha_type=challenge.captcha_type,
        sitekey=challenge.sitekey,
        page_url=challenge.page_url,
    )

    base_url = (captcha_solver_url or "https://2captcha.com").rstrip("/")
    method = CaptchaSolver._resolve_method(challenge.captcha_type)

    try:
        resp = sync_http_client.post(
            f"{base_url}/in.php",
            data={
                "key": captcha_solver_api_key,
                "method": method,
                "googlekey": challenge.sitekey,
                "pageurl": challenge.page_url,
                "json": "1",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != 1:
            log_event(
                logging.getLogger("captcha_solver"),
                "captcha.failed",
                url=target_url,
                error=f"Submit failed: {data.get('request', 'unknown')}",
            )
            return None

        task_id = str(data["request"])
        log_event(
            logging.getLogger("captcha_solver"),
            "captcha.sent",
            task_id=task_id,
            captcha_type=challenge.captcha_type,
        )

        start = time.monotonic()
        while time.monotonic() - start < 180:
            time.sleep(5)
            poll_resp = sync_http_client.get(
                f"{base_url}/res.php",
                params={
                    "key": captcha_solver_api_key,
                    "action": "get",
                    "id": task_id,
                    "json": "1",
                },
                timeout=30,
            )
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()
            if poll_data.get("status") == 1:
                token = str(poll_data["request"])
                log_event(
                    logging.getLogger("captcha_solver"),
                    "captcha.solved",
                    task_id=task_id,
                    elapsed_seconds=round(time.monotonic() - start, 1),
                )

                cookie_name = _resolve_cookie_name(challenge.captcha_type)
                retry_resp = sync_http_client.get(
                    target_url,
                    timeout=timeout,
                    follow_redirects=True,
                    cookies={cookie_name: token},
                )
                if retry_resp.status_code == 200:
                    ct = retry_resp.headers.get("content-type", "").lower()
                    if "pdf" in ct or "octet-stream" in ct or not ct.startswith("text/html"):
                        return retry_resp.content
                return None

            error_text = poll_data.get("request", "")
            if error_text == "CAPCHA_NOT_READY":
                continue
            if error_text == "ERROR_CAPTCHA_UNSOLVABLE":
                log_event(
                    logging.getLogger("captcha_solver"),
                    "captcha.failed",
                    url=target_url,
                    error="Unsolvable",
                )
                return None
            if error_text.startswith("ERROR"):
                log_event(
                    logging.getLogger("captcha_solver"),
                    "captcha.failed",
                    url=target_url,
                    error=error_text,
                )
                return None

        log_event(
            logging.getLogger("captcha_solver"),
            "captcha.failed",
            url=target_url,
            error="Timeout after 180s",
        )
        return None

    except Exception as exc:
        log_event(
            logging.getLogger("captcha_solver"),
            "captcha.failed",
            url=target_url,
            error=str(exc),
        )
        return None


def _resolve_cookie_name(captcha_type: str) -> str:
    mapping = {
        "recaptcha_v2": "recaptcha-token",
        "hcaptcha": "hcaptcha-token",
        "pravocaptcha": "pravocaptcha-token",
    }
    return mapping.get(captcha_type, "captcha-token")

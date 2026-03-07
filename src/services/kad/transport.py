import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from src.app.bot_logging import log_debug, log_event
from src.domain.kad_models import (
    KadAccessError,
    KadInvalidResponseError,
    KadRateLimitError,
    KadUnavailableError,
    RequestResult,
)


class KadTransport:
    """Handles HTTP communication, retries, and rate limiting for the KAD API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 30,
        sync_http_client: Optional[httpx.Client] = None,
        async_http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout_seconds

        limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
        self.sync_client = sync_http_client or httpx.Client(
            timeout=self.timeout, limits=limits
        )
        self.async_client = async_http_client or httpx.AsyncClient(
            timeout=self.timeout, limits=limits
        )

        self.owns_sync = sync_http_client is None
        self.owns_async = async_http_client is None
        self.logger = logging.getLogger("kad_transport")

    async def aclose(self) -> None:
        if self.owns_async:
            await self.async_client.aclose()
        if self.owns_sync:
            self.sync_client.close()

    def request_json(self, method: str, path: str, params: Dict[str, Any]) -> dict:
        url = f"{self.base_url}{path}"
        rate_limit_attempts = 0
        unavailable_attempts = 0
        last_error = None

        for _ in range(5):
            try:
                response = self.sync_client.request(
                    method, url, params=params, timeout=self.timeout
                )
                if response.status_code == 429:
                    rate_limit_attempts += 1
                    if rate_limit_attempts >= 3:
                        raise KadRateLimitError("KAD API rate limit exceeded")
                    log_event(
                        self.logger, "kad.rate_limit", attempt=rate_limit_attempts
                    )
                    import time

                    time.sleep(60)
                    continue
                if response.status_code in {503, 504, 500}:
                    unavailable_attempts += 1
                    if unavailable_attempts >= 2:
                        raise KadUnavailableError(
                            f"KAD API unavailable: {response.status_code}"
                        )
                    log_event(
                        self.logger, "kad.unavailable", attempt=unavailable_attempts
                    )
                    continue
                if response.status_code == 403:
                    raise KadAccessError(
                        str(response.json().get("error", "Access denied"))
                    )
                if response.status_code == 400:
                    raise KadInvalidResponseError(
                        str(response.json().get("error", "Invalid request"))
                    )

                response.raise_for_status()
                data = response.json()
                if data.get("Success") == 0 and "error" not in data:
                    log_debug(self.logger, "kad.success_zero")
                    return data
                return data
            except httpx.TimeoutException as exc:
                unavailable_attempts += 1
                last_error = exc
                if unavailable_attempts >= 2:
                    raise KadUnavailableError("KAD API timeout") from exc
            except httpx.HTTPError as exc:
                last_error = exc

        raise KadInvalidResponseError("KAD API request failed") from last_error

    async def request_json_async(
        self, method: str, path: str, params: Dict[str, Any]
    ) -> RequestResult:
        url = f"{self.base_url}{path}"
        rate_limit_attempts = 0
        unavailable_attempts = 0
        retry_count = 0
        had_transient = False
        last_error = None

        for _ in range(5):
            try:
                response = await self.async_client.request(
                    method, url, params=params, timeout=self.timeout
                )
                if response.status_code == 429:
                    retry_count += 1
                    had_transient = True
                    rate_limit_attempts += 1
                    if rate_limit_attempts >= 3:
                        raise KadRateLimitError("KAD API rate limit")
                    log_event(
                        self.logger, "kad.rate_limit", attempt=rate_limit_attempts
                    )
                    await asyncio.sleep(60)
                    continue
                if response.status_code in {503, 504, 500}:
                    retry_count += 1
                    had_transient = True
                    unavailable_attempts += 1
                    if unavailable_attempts >= 2:
                        raise KadUnavailableError(
                            f"KAD API unavailable: {response.status_code}"
                        )
                    log_event(
                        self.logger, "kad.unavailable", attempt=unavailable_attempts
                    )
                    continue
                if response.status_code == 403:
                    raise KadAccessError(
                        str(response.json().get("error", "Access denied"))
                    )
                if response.status_code == 400:
                    raise KadInvalidResponseError(
                        str(response.json().get("error", "Invalid request"))
                    )

                response.raise_for_status()
                data = response.json()

                if data.get("Success") == 0 and "error" not in data:
                    if "details_by_id" in path:
                        # Silent drop retry logic for details API
                        retry_count += 1
                        had_transient = True
                        unavailable_attempts += 1
                        log_event(
                            self.logger,
                            "kad.silent_drop_retry",
                            path=path,
                            attempt=unavailable_attempts,
                        )
                        if unavailable_attempts < 2:
                            await asyncio.sleep(2)
                            continue
                        raise KadUnavailableError(
                            "KAD API silently dropping case details"
                        )
                    log_debug(self.logger, "kad.success_zero")

                return RequestResult(
                    data=data,
                    retry_count=retry_count,
                    had_transient_error=had_transient,
                )
            except httpx.TimeoutException as exc:
                retry_count += 1
                had_transient = True
                unavailable_attempts += 1
                last_error = exc
                if unavailable_attempts >= 2:
                    raise KadUnavailableError("KAD API timeout") from exc
            except httpx.HTTPError as exc:
                last_error = exc

        raise KadInvalidResponseError("KAD API request failed") from last_error

    def validate_success(self, data: dict) -> dict:
        if "error" in data:
            raise KadAccessError(str(data.get("error")))
        return data

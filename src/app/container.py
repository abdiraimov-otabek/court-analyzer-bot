from __future__ import annotations

import httpx

from src.app.config import AppConfig
from src.core.bot_logic import BotLogic
from src.core.estimation import estimate_minutes
from src.domain.analysis import AnalysisService
from src.infrastructure.access_repository import AllowedUsersRepository
from src.infrastructure.access_store import DatabaseAccessStore
from src.infrastructure.active_requests_repository import ActiveRequestsRepository
from src.infrastructure.cache_repository import AnalysisCacheRepository
from src.infrastructure.case_details_cache_repository import CaseDetailsCacheRepository
from src.infrastructure.log_repository import LogRepository
from src.infrastructure.rate_limit_repository import LoginRateLimitRepository
from src.infrastructure.session_repository import AdminSessionRepository
from src.infrastructure.settings_repository import SettingsRepository
from src.infrastructure.sqlite import SqliteConnection
from src.services.access_control import AccessControlList
from src.services.active_requests import ActiveRequestRegistry
from src.services.hashing import HashingService
from src.services.postgres_kad_client import PostgresKadClient
from src.services.llm_reason_extractor import LLMReasonExtractor
from src.services.quarter_selection import QuarterSelectionRegistry
from src.services.rate_limit import HourlyRateLimiter
from src.services.request_processor import RequestProcessor
from src.services.settings_service import SettingsService


class Container:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        from src.app.config import initialize_db
        initialize_db(config)
        self.connection = SqliteConnection(config.database_path)
        self.settings_repository = SettingsRepository(self.connection)
        self.settings_service = SettingsService(self.settings_repository)
        self.allowed_users_repository = AllowedUsersRepository(self.connection)
        self.access_control = AccessControlList(
            DatabaseAccessStore(self.allowed_users_repository)
        )
        db_repo = ActiveRequestsRepository(self.connection)
        self.active_requests = ActiveRequestRegistry(db_repo=db_repo)
        self.quarter_selections = QuarterSelectionRegistry()
        self.rate_limiter = HourlyRateLimiter(limit=10)
        self.cache_repository = AnalysisCacheRepository(
            self.connection, ttl_seconds=24 * 60 * 60
        )
        self.case_details_cache_repository = CaseDetailsCacheRepository(
            self.connection, ttl_seconds=24 * 60 * 60
        )
        self.log_repository = LogRepository(self.connection)
        self.session_repository = AdminSessionRepository(self.connection)
        self.rate_limit_repository = LoginRateLimitRepository(self.connection)
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
        self.sync_http_client = httpx.Client(timeout=30, limits=limits)
        self.async_http_client = httpx.AsyncClient(timeout=30, limits=limits)
        self._kad_client: PostgresKadClient | None = None
        self._llm_extractor: LLMReasonExtractor | None = None

    def build_bot_logic(self) -> BotLogic:
        return BotLogic(
            access_control=self.access_control,
            rate_limiter=self.rate_limiter,
            active_requests=self.active_requests,
            quarter_selections=self.quarter_selections,
            count_provider=self._get_kad_client(),
            settings_provider=self.settings_service,
            estimate_minutes=estimate_minutes,
        )

    def build_request_processor(self) -> RequestProcessor:
        llm_extractor = self._get_llm_reason_extractor()
        return RequestProcessor(
            kad_client=self._get_kad_client(),
            analysis_service=AnalysisService(llm_reason_extractor=llm_extractor),
            active_requests=self.active_requests,
            cache_repository=self.cache_repository,
            log_repository=self.log_repository,
            hashing_service=self._build_hashing_service(),
        )

    def _get_kad_client(self) -> PostgresKadClient:
        if self._kad_client is None:
            self._kad_client = self._build_kad_client()
        return self._kad_client

    def _build_kad_client(self) -> PostgresKadClient:
        return PostgresKadClient(
            llm_reason_extractor=self._get_llm_reason_extractor(),
        )

    def _get_llm_reason_extractor(self) -> LLMReasonExtractor | None:
        if self._llm_extractor is not None:
            return self._llm_extractor
        if self.config.openrouter_api_key:
            self._llm_extractor = LLMReasonExtractor(
                http_client=self.async_http_client,
                api_key=self.config.openrouter_api_key,
            )
            return self._llm_extractor
        return None

    def _build_hashing_service(self) -> HashingService:
        if not self.config.hash_salt:
            raise RuntimeError("HASH_SALT is not configured")
        return HashingService(self.config.hash_salt)

    async def aclose(self) -> None:
        if self._kad_client is not None:
            await self._kad_client.aclose()
        await self.async_http_client.aclose()
        self.sync_http_client.close()

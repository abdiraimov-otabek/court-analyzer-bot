import asyncio

from src.app.config import AppConfig
from src.app.container import Container


def test_container_reuses_same_rate_limiter_instance():
    container = Container(
        AppConfig(
            database_path=":memory:",
            hash_salt="salt",
            telegram_bot_token="token",
            kad_api_base_url="https://example.com",
            kad_api_key="key",
            admin_auth_token="admin",
        )
    )

    logic_a = container.build_bot_logic()
    logic_b = container.build_bot_logic()

    assert logic_a._rate_limiter is logic_b._rate_limiter
    asyncio.run(container.aclose())

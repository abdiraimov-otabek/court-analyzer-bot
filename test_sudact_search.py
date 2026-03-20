import asyncio
from src.services.sudact_client import SudactClient
from src.domain.settings import Settings

async def main():
    client = SudactClient(concurrency=1, page_concurrency=1)
    
    settings = Settings(
        max_cases=50,
        max_documents_per_case=5,
        max_pages=80,
        fetch_concurrency_min=1,
        fetch_concurrency_max=1,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=3600,
        analysis_prompt="",
        updated_at=None
    )
    
    result = await client.fetch_decisions("ст. 61.3 банкротство", settings)
    print(f"Collected links: {len(result.decisions)}")
    await client.aclose()

asyncio.run(main())

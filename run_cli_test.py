import asyncio
import os
import sys

# Ensure src in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from src.app.config import load_config, initialize_db
from src.app.container import Container
from src.domain.value_objects import UserId
from src.domain.settings import Settings

async def main():
    from src.app.bot_logging import configure_logging
    configure_logging("test_sim")
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Initializing components...")
    
    # Use a dedicated test database to avoid locks/IO errors with active bot
    os.environ["DATABASE_PATH"] = "data/test_sim.db"
    
    config = load_config()
    initialize_db(config)
    container = Container(config)
    processor = container.build_request_processor()
    settings_service = container.settings_service
    
    print(f"KAD API Key: {'Set' if config.kad_api_key else 'MISSING'}")
    print(f"OpenRouter API Key: {'Set' if config.openrouter_api_key else 'MISSING'}")
    
    import dataclasses
    base_settings = settings_service.get_settings()
    test_settings = dataclasses.replace(
        base_settings, 
        max_cases=50,
        llm_model="google/gemini-2.0-flash-001",
        fast_llm_model="google/gemini-2.0-flash-001"
    )
    
    user_id = UserId(99999999) # Fake user ID
    query = "Практика АС Москвы по статье 61.2 Закона о банкротстве за 2024 год"
    
    print(f"\n=============================================")
    print(f"Executing End-to-End Test")
    print(f"Query: {query}")
    print(f"Max cases: {test_settings.max_cases}")
    print(f"=============================================\n")
    
    try:
        # Start processing
        result = await processor.process(user_id, query, test_settings)
        
        print("\n=============================================")
        print("TEST COMPLETED SUCCESSFULLY")
        print("=============================================\n")
        print("Summary:")
        print(result.summary)
        print("\nRelevant Cases:")
        for case in result.decisions:
            print(f"- {case.case_number}: Outcome={case.outcome.value}, Confidence={case.validation_confidence.value}")
            print(f"  Reasons: {'; '.join(case.reasons)}")
            print(f"  Proof: {case.proof_quote}")
            print()
            
    except Exception as e:
        print(f"\nPipeline failed with error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())

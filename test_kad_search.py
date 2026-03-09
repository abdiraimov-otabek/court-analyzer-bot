import asyncio
from src.app.config import load_config
from src.app.container import Container
from src.domain.kad_models import SearchParams
from src.domain.settings import default_settings
from datetime import datetime

async def main():
    config = load_config()
    container = Container(config)
    client = container._get_kad_client()
    
    case_num = "А41-117294/2024"
    print(f"Fetching details for CaseNumber={case_num} with max_documents_per_case=100...")
    decision = client._fetch_case_by_number(case_num, max_documents_per_case=100)
    
    if decision:
        print(f"Case Number: {decision.case_number}")
        print(f"Decision Date: {decision.decision_date}")
        print(f"Analysis Text Length: {len(decision.analysis_text)}")
        print(f"Outcome: {decision.outcome}")
        print(f"Reasons: {decision.reasons}")
        print(f"Matched Article: {getattr(decision, 'matched_article', 'N/A')}")
        print("\n--- Analysis Text (Full) ---\n")
        print(decision.analysis_text)
    else:
        print("Case not found.")
    
    await container.aclose()

if __name__ == "__main__":
    asyncio.run(main())    

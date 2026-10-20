"""
Test script for fetching budget summary data from HCSS HeavyJob API.
This script helps verify the HCSS API integration and save sample responses.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

# Add repo root to path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from budget_tracking.hcss_client import BudgetHCSSClient
from budget_tracking_apis.response_saver import save_response


def main():
    """Fetch budget summary data and save to file."""
    print("=" * 80)
    print("HCSS Budget Summary API Test")
    print("=" * 80)
    
    # Use yesterday as default date
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    
    print(f"\nFetching budget summary for date: {yesterday}")
    print("This may take a moment...\n")
    
    try:
        client = BudgetHCSSClient()
        
        # Fetch budget summary for yesterday
        results = client.fetch_budget_summary(date=yesterday)
        
        print(f"✓ Successfully fetched {len(results)} budget items")
        
        # Save response to file
        output_file = save_response("get_budget_summary", results)
        print(f"✓ Response saved to: {output_file}")
        
        # Display sample items
        if results:
            print("\n" + "=" * 80)
            print("Sample Budget Items (first 3):")
            print("=" * 80)
            
            for i, item in enumerate(results[:3], 1):
                print(f"\n--- Item {i} ---")
                print(f"Job: {item.get('jobName', 'N/A')} ({item.get('jobId', 'N/A')})")
                print(f"Cost Code: {item.get('code', 'N/A')} - {item.get('description', 'N/A')}")
                print(f"Business Unit: {item.get('businessUnit', 'N/A')}")
                print(f"Expected Budget: ${item.get('expectedBudget', 0):,.2f}")
                print(f"Actual Cost: ${item.get('actualCost', 0):,.2f}")
                
                # Calculate utilization
                expected = item.get('expectedBudget', 0)
                actual = item.get('actualCost', 0)
                if expected > 0:
                    utilization = (actual / expected) * 100
                    print(f"Utilization: {utilization:.1f}%")
                    status = "ON_TRACK" if utilization < 85 else "OVER_RISK"
                    print(f"Status: {status}")
        
        print("\n" + "=" * 80)
        print("✓ Test completed successfully!")
        print("=" * 80)
        
    except Exception as exc:
        print(f"\n✗ Error: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

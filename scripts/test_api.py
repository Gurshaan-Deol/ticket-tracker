"""
Phase 5 API test script.

Start the server first:
  uvicorn app.main:app --reload --log-level debug

Then run:
  python scripts/test_api.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json

BASE_URL = "http://localhost:8000"


async def main() -> None:
    try:
        import httpx
    except ImportError:
        print("httpx not installed — run: pip install httpx")
        sys.exit(1)

    print("Start the server first with: uvicorn app.main:app --reload\n")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        # a. Health check
        print("--- GET /api/health ---")
        try:
            r = await client.get("/api/health")
            data = r.json()
            print(f"  status: {data.get('status')}")
            print(f"  scheduler_running: {data.get('scheduler_running')}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # b. Dashboard
        print("\n--- GET / ---")
        try:
            r = await client.get("/")
            print(f"  HTTP {r.status_code}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # c. AI search
        print("\n--- POST /events/search ---")
        try:
            r = await client.post(
                "/events/search",
                json={"query": "Bruno Mars Toronto"},
                headers={"Content-Type": "application/json"},
            )
            print(f"  HTTP {r.status_code}")
            print(f"  {json.dumps(r.json(), indent=2)}")
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\nAPI test complete")


asyncio.run(main())

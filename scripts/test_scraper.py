import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from app.scraper.ticketmaster import scrape_event


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.ticketmaster.ca/bruno-mars-the-romantic-tour-toronto-ontario-05-23-2026/event/10006390B4647809"
    )

    print(f"\nScraping: {url}")
    start = time.monotonic()
    results, available_quantities, raw_offers = await scrape_event(url, quantity=None)
    elapsed = time.monotonic() - start

    print(f"\nDone in {elapsed:.1f}s — {len(results)} section(s)")
    print(f"Available quantities: {available_quantities}")
    for r in results[:10]:
        print(f"  {r.name}: ${r.min_price:.2f}")

    print(f"\nRaw offers captured: {len(raw_offers)}")
    sq_values = {}
    for r in raw_offers:
        sq_values[r.sellable_quantities] = sq_values.get(r.sellable_quantities, 0) + 1
    print("sellable_quantities breakdown:")
    for sq, count in sorted(sq_values.items()):
        print(f"  '{sq}': {count} offers")


asyncio.run(main())

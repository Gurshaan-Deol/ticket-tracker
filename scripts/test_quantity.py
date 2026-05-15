import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from app.scraper.ticketmaster import scrape_event

async def main():
    url = "https://www.ticketmaster.ca/bruno-mars-the-romantic-tour-toronto-ontario-05-23-2026/event/10006390B4647809"

    print("\nScraping with quantity=1...")
    results_1, _ = await scrape_event(url, quantity=1)
    print(f"quantity=1: {len(results_1)} sections")
    for r in results_1[:5]:
        print(f"  {r.name}: ${r.min_price:.2f}")

    print("\nScraping with quantity=2...")
    results_2, _ = await scrape_event(url, quantity=2)
    print(f"quantity=2: {len(results_2)} sections")
    for r in results_2[:5]:
        print(f"  {r.name}: ${r.min_price:.2f}")

    print("\nScraping with quantity=4...")
    results_4, _ = await scrape_event(url, quantity=4)
    print(f"quantity=4: {len(results_4)} sections")
    for r in results_4[:5]:
        print(f"  {r.name}: ${r.min_price:.2f}")

    print("\nScraping with quantity=None (no filter)...")
    results_none, avail = await scrape_event(url, quantity=None)
    print(f"quantity=None: {len(results_none)} sections")
    print(f"Available quantities: {avail}")

asyncio.run(main())

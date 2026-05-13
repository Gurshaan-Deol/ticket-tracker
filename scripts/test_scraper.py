"""
Quick smoke-test for the scraper. Run against a real Ticketmaster URL:

    python scripts/test_scraper.py <ticketmaster_url>
"""
import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is on the path regardless of where the script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scraper.ticketmaster import scrape_event  # noqa: E402

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
)


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_scraper.py <ticketmaster_url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"\nScraping: {url}\n")

    try:
        results, api_count = await scrape_event(url)
    except Exception as exc:
        print(f"ERROR: scraping raised an exception — {exc}")
        sys.exit(1)

    print(f"Captured {api_count} API response(s)")

    if not results:
        print("ERROR: scrape succeeded but returned 0 listings.")
        sys.exit(1)

    for r in results:
        print(f"  {r.name}: ${r.min_price:.2f}")

    print(f"\nTotal: {len(results)} listing(s)")


if __name__ == "__main__":
    asyncio.run(main())

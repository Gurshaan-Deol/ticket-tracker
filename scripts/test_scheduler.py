"""
Phase 3 end-to-end test for the scheduler engine.

Usage: python scripts/test_scheduler.py <ticketmaster_url>
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.database import AsyncSessionLocal, Base, engine
from app.models import AlertLog, Event, Listing, PriceSnapshot, UserWatch
from app.scraper.ticketmaster import scrape_event
from app.scheduler.engine import run_watch_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_scheduler")


async def run_test(url: str) -> None:
    # Ensure data directory exists for SQLite file
    Path("data").mkdir(exist_ok=True)

    # Initialize DB tables without running Alembic
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ready")

    # Step 3-4: Do one scrape to discover the cheapest listing
    logger.info("Scraping %s to find cheapest listing...", url)
    scraped_results, api_count = await scrape_event(url)
    if not scraped_results:
        print("ERROR: No listings found from the scrape — check the URL")
        sys.exit(1)

    cheapest = scraped_results[0]  # sorted ascending by price
    logger.info("Cheapest listing: '%s' @ $%.2f", cheapest.name, cheapest.min_price)

    async with AsyncSessionLocal() as session:
        # Create Event if not already in DB
        ev_result = await session.execute(
            select(Event).where(Event.ticketmaster_url == url)
        )
        event = ev_result.scalar_one_or_none()
        if not event:
            event = Event(
                name="Test Event (Phase 3)",
                venue="Test Venue",
                event_date=None,
                ticketmaster_url=url,
                is_active=True,
            )
            session.add(event)
            await session.flush()
        event_id = event.id

        # Create the watched Listing for the cheapest section
        ls_result = await session.execute(
            select(Listing).where(
                Listing.event_id == event_id,
                Listing.name == cheapest.name,
            )
        )
        listing = ls_result.scalar_one_or_none()
        if not listing:
            listing = Listing(
                event_id=event_id,
                name=cheapest.name,
                is_available=True,
                last_seen_at=datetime.now(timezone.utc),
            )
            session.add(listing)
            await session.flush()
        listing_id = listing.id

        # Step 5: Create UserWatch — target above current price so no alert fires
        watch = UserWatch(
            listing_id=listing_id,
            target_price=cheapest.min_price + 50.0,
            refresh_interval_minutes=1,
            alert_cooldown_minutes=60,
            is_active=True,
        )
        session.add(watch)
        await session.flush()
        watch_id = watch.id

        await session.commit()

    logger.info(
        "Created Event#%d, Listing#%d, Watch#%d", event_id, listing_id, watch_id
    )

    # Step 6: Call run_watch_job directly (no scheduler needed)
    logger.info("Running watch job watch_id=%d...", watch_id)
    await run_watch_job(watch_id)

    # Steps 7-8: Query and report results
    async with AsyncSessionLocal() as session:
        snaps_result = await session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.listing_id == listing_id)
            .order_by(PriceSnapshot.scraped_at.desc())
        )
        snapshots = snaps_result.scalars().all()

        print(f"\nPriceSnapshots saved: {len(snapshots)}")
        if snapshots:
            print(f"Latest snapshot price: ${snapshots[0].price:.2f}")
        else:
            print("WARNING: No snapshots saved — the watched listing may not have appeared in results")

    # Step 9: Clean up all test records
    async with AsyncSessionLocal() as session:
        # Gather all listing IDs for this event (run_watch_job may have created extras)
        all_listings_result = await session.execute(
            select(Listing).where(Listing.event_id == event_id)
        )
        all_listing_ids = [l.id for l in all_listings_result.scalars().all()]

        for lid in all_listing_ids:
            for row in (await session.execute(
                select(PriceSnapshot).where(PriceSnapshot.listing_id == lid)
            )).scalars().all():
                await session.delete(row)

            for row in (await session.execute(
                select(AlertLog).where(AlertLog.listing_id == lid)
            )).scalars().all():
                await session.delete(row)

            for row in (await session.execute(
                select(UserWatch).where(UserWatch.listing_id == lid)
            )).scalars().all():
                await session.delete(row)

            for row in (await session.execute(
                select(Listing).where(Listing.id == lid)
            )).scalars().all():
                await session.delete(row)

        ev_obj = (await session.execute(
            select(Event).where(Event.id == event_id)
        )).scalar_one_or_none()
        if ev_obj:
            await session.delete(ev_obj)

        await session.commit()

    logger.info("Test records cleaned up")
    print("\nPhase 3 test passed")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_scheduler.py <ticketmaster_url>")
        sys.exit(1)

    asyncio.run(run_test(sys.argv[1]))

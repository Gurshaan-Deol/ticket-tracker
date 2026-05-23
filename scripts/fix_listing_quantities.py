import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, delete
from app.database import AsyncSessionLocal
from app.models import Event, Listing, PriceSnapshot
from app.scraper.ticketmaster import scrape_event_sync

async def fix():
    async with AsyncSessionLocal() as session:
        # Get all events
        result = await session.execute(select(Event))
        events = result.scalars().all()

        for event in events:
            print(f"Processing: {event.name}")

            # Scrape fresh with no quantity filter
            try:
                scraped_results, _ = scrape_event_sync(event.ticketmaster_url)
            except Exception as e:
                print(f"  Scrape failed: {e}")
                continue

            # Delete all existing listings (and their snapshots cascade)
            # for this event — we'll recreate them with quantities
            existing = await session.execute(
                select(Listing).where(Listing.event_id == event.id)
            )
            existing_listings = existing.scalars().all()
            existing_ids = [l.id for l in existing_listings]

            if existing_ids:
                await session.execute(
                    delete(PriceSnapshot)
                    .where(PriceSnapshot.listing_id.in_(existing_ids))
                )
                await session.execute(
                    delete(Listing).where(Listing.event_id == event.id)
                )

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)

            # Recreate listings with quantity populated
            listing_map = {}
            for r in scraped_results:
                listing = Listing(
                    event_id=event.id,
                    name=r.name.strip(),
                    quantity=r.quantity,
                    is_available=True,
                    last_seen_at=now,
                )
                session.add(listing)

            await session.flush()

            saved = await session.execute(
                select(Listing).where(Listing.event_id == event.id)
            )
            for l in saved.scalars().all():
                listing_map[(l.name.strip().lower(), l.quantity)] = l.id

            for r in scraped_results:
                lid = listing_map.get((r.name.strip().lower(), r.quantity))
                if lid and r.min_price > 0:
                    session.add(PriceSnapshot(
                        listing_id=lid,
                        price=r.min_price,
                        scraped_at=now,
                    ))

            await session.commit()
            print(f"  Done — {len(scraped_results)} listings saved")

asyncio.run(fix())

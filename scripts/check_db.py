import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from app.database import AsyncSessionLocal
from app.models import Event, Listing, PriceSnapshot, UserWatch
from sqlalchemy import select, func

async def main():
    async with AsyncSessionLocal() as db:
        # Show all events and their quantity setting
        events = (await db.execute(select(Event))).scalars().all()
        for event in events:
            print(f"\nEvent: {event.name}")
            print(f"  quantity: {event.quantity}")
            print(f"  available_quantities: {event.available_quantities}")

            # Show listing count and snapshot count
            listings = (await db.execute(
                select(Listing).where(Listing.event_id == event.id)
            )).scalars().all()
            print(f"  listings: {len(listings)}")

            total_snaps = 0
            for listing in listings:
                snaps = (await db.execute(
                    select(PriceSnapshot)
                    .where(PriceSnapshot.listing_id == listing.id)
                    .order_by(PriceSnapshot.scraped_at.desc())
                    .limit(1)
                )).scalars().all()
                if snaps:
                    total_snaps += 1
                    print(f"  {listing.name}: ${snaps[0].price:.2f} (scraped_at: {snaps[0].scraped_at})")

asyncio.run(main())

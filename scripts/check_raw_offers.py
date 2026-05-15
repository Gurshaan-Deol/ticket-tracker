import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from app.database import AsyncSessionLocal
from app.models import Event, RawOffer
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as db:
        events = (await db.execute(select(Event))).scalars().all()
        for event in events:
            print(f"\nEvent: {event.name} (quantity={event.quantity})")

            raw = (await db.execute(
                select(RawOffer).where(RawOffer.event_id == event.id)
            )).scalars().all()

            print(f"Total raw offers stored: {len(raw)}")

            # Show breakdown of sellable_quantities values
            sq_values = {}
            for r in raw:
                sq_values[r.sellable_quantities] = sq_values.get(r.sellable_quantities, 0) + 1
            print("sellable_quantities breakdown:")
            for sq, count in sorted(sq_values.items()):
                print(f"  '{sq}': {count} offers")

            # Show how many would pass quantity=5 filter
            passing_5 = []
            for r in raw:
                sq = r.sellable_quantities.strip()
                if sq == "any" or not sq:
                    passing_5.append(r)
                else:
                    nums = [int(q) for q in sq.split(",") if q.strip().isdigit()]
                    if 5 in nums:
                        passing_5.append(r)

            print(f"\nOffers passing quantity=5 filter: {len(passing_5)}")

            # Show unique sections for qty=5
            sections_5 = {}
            for r in passing_5:
                key = r.section.strip().lower()
                if key not in sections_5 or r.list_price < sections_5[key]:
                    sections_5[key] = r.list_price
            print(f"Unique sections for qty=5: {len(sections_5)}")
            for s, p in sorted(sections_5.items(), key=lambda x: x[1])[:10]:
                print(f"  {s}: ${p:.2f}")

asyncio.run(main())

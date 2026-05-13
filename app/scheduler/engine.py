import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import AlertLog, Event, Listing, PriceSnapshot, UserWatch
from app.scraper.ticketmaster import scrape_event

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def start_scheduler() -> None:
    scheduler.start()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserWatch).where(UserWatch.is_active == True))
        watches = result.scalars().all()
        for watch in watches:
            schedule_watch_job(watch)
    logger.info("Scheduler started — %d job(s) scheduled", len(watches))


async def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler shut down")


def schedule_watch_job(watch: UserWatch) -> None:
    job_id = f"watch_{watch.id}"
    scheduler.add_job(
        run_watch_job,
        trigger=IntervalTrigger(minutes=watch.refresh_interval_minutes),
        id=job_id,
        args=[watch.id],
        replace_existing=True,
    )
    logger.info("Scheduled job %s (every %d min)", job_id, watch.refresh_interval_minutes)


def remove_watch_job(watch_id: int) -> None:
    job_id = f"watch_{watch_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info("Removed job %s", job_id)
    except Exception:
        logger.warning("Job %s not found — nothing to remove", job_id)


async def run_watch_job(watch_id: int) -> None:
    session = AsyncSessionLocal()
    try:
        # Load watch first to get interval for jitter calculation
        result = await session.execute(select(UserWatch).where(UserWatch.id == watch_id))
        watch = result.scalar_one_or_none()
        if not watch or not watch.is_active:
            logger.warning("Watch %d not found or inactive — skipping", watch_id)
            return

        # Step 1: Jitter — staggers concurrent jobs
        jitter_seconds = random.uniform(0, watch.refresh_interval_minutes * 0.15 * 60)
        logger.debug("Watch %d jitter: %.1fs", watch_id, jitter_seconds)
        await asyncio.sleep(jitter_seconds)

        # Step 4: Load Listing and Event
        listing_result = await session.execute(select(Listing).where(Listing.id == watch.listing_id))
        listing = listing_result.scalar_one_or_none()
        if not listing:
            logger.warning("Listing %d not found for watch %d", watch.listing_id, watch_id)
            return

        event_result = await session.execute(select(Event).where(Event.id == listing.event_id))
        event = event_result.scalar_one_or_none()
        if not event or not event.is_active:
            logger.info("Event %d inactive — skipping watch %d", listing.event_id, watch_id)
            return

        # Step 5: Scrape
        try:
            scraped_results, _ = await scrape_event(event.ticketmaster_url)
        except Exception as e:
            logger.error("Scrape failed for watch %d: %s", watch_id, e)
            return

        now = datetime.now(timezone.utc)
        scraped_names_lower = {r.name.strip().lower() for r in scraped_results}

        # Step 6: Find the matching listing by name
        matched_result = None
        for r in scraped_results:
            if r.name.strip().lower() == listing.name.strip().lower():
                matched_result = r
                break

        # Step 7: Reconcile all listings for this event
        all_db_result = await session.execute(
            select(Listing).where(Listing.event_id == event.id)
        )
        all_db_listings = all_db_result.scalars().all()
        known_by_name = {l.name.strip().lower(): l for l in all_db_listings}

        for r in scraped_results:
            key = r.name.strip().lower()
            if key in known_by_name:
                known_by_name[key].last_seen_at = now
                known_by_name[key].is_available = True
            else:
                session.add(Listing(
                    event_id=event.id,
                    name=r.name.strip(),
                    is_available=True,
                    last_seen_at=now,
                ))

        for db_listing in all_db_listings:
            if db_listing.name.strip().lower() not in scraped_names_lower:
                db_listing.is_available = False

        # Step 8: Save snapshot if the watched listing appeared in results
        if matched_result:
            current_price = matched_result.min_price
            logger.info("Watch %d — %s: $%.2f", watch_id, listing.name, current_price)

            # Query previous snapshot BEFORE adding the new one to avoid ordering ambiguity
            prev_result = await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.listing_id == listing.id)
                .order_by(PriceSnapshot.scraped_at.desc())
                .limit(1)
            )
            previous_snapshot = prev_result.scalar_one_or_none()

            session.add(PriceSnapshot(
                listing_id=listing.id,
                price=current_price,
                scraped_at=now,
            ))

            # Step 9: Alert check — only after at least one prior snapshot exists
            if previous_snapshot and current_price < watch.target_price:
                cooldown_cutoff = now - timedelta(minutes=watch.alert_cooldown_minutes)
                recent_result = await session.execute(
                    select(AlertLog)
                    .where(AlertLog.listing_id == listing.id)
                    .where(AlertLog.alerted_at > cooldown_cutoff)
                    .limit(1)
                )
                if not recent_result.scalar_one_or_none():
                    await fire_alert(watch, listing, event, current_price)
                    session.add(AlertLog(
                        listing_id=listing.id,
                        price_at_alert=current_price,
                        alerted_at=now,
                        channels_used="stub",
                    ))
        else:
            logger.info(
                "Watch %d — listing '%s' not found in scraped results", watch_id, listing.name
            )

        # Step 10: Commit
        await session.commit()
        logger.info("Watch %d job complete", watch_id)

    except Exception as e:
        logger.error("run_watch_job(%d) failed: %s", watch_id, e, exc_info=True)
    finally:
        await session.close()


async def fire_alert(watch, listing, event, price: float) -> None:
    # TODO: wire to notifier in Phase 4
    logger.info(
        f"ALERT (stub): {listing.name} on {event.name} dropped to ${price:.2f} "
        f"(target: ${watch.target_price:.2f})"
    )

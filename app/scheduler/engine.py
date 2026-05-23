import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import AlertLog, AvailabilityWatch, Event, Listing, PriceSnapshot, UserWatch
from app.scraper.ticketmaster import scrape_event, scrape_event_sync

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def start_scheduler() -> None:
    scheduler.start()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserWatch).where(UserWatch.is_active == True))
        watches = result.scalars().all()
        for watch in watches:
            schedule_watch_job(watch)

        events_result = await session.execute(
            select(AvailabilityWatch.event_id)
            .where(AvailabilityWatch.is_active == True)
            .distinct()
        )
        av_event_ids = [row[0] for row in events_result.all()]
        for event_id in av_event_ids:
            schedule_availability_job(event_id)

    logger.info(
        "Scheduler started — %d watch job(s), %d availability job(s) scheduled",
        len(watches), len(av_event_ids),
    )


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
        misfire_grace_time=300,
        max_instances=1,
    )
    logger.info("Scheduled job %s (every %d min)", job_id, watch.refresh_interval_minutes)


def remove_watch_job(watch_id: int) -> None:
    job_id = f"watch_{watch_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info("Removed job %s", job_id)
    except Exception:
        logger.warning("Job %s not found — nothing to remove", job_id)


def schedule_availability_job(event_id: int) -> None:
    job_id = f"availability_{event_id}"
    scheduler.add_job(
        run_availability_check,
        trigger=IntervalTrigger(minutes=30),
        id=job_id,
        args=[event_id],
        replace_existing=True,
        misfire_grace_time=300,
        max_instances=1,
    )
    logger.info("Scheduled availability job for event %d", event_id)


def remove_availability_job(event_id: int) -> None:
    job_id = f"availability_{event_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info("Removed availability job for event %d", event_id)
    except Exception:
        pass


async def run_availability_check(event_id: int) -> None:
    async with AsyncSessionLocal() as session:
        try:
            event_result = await session.execute(
                select(Event).where(Event.id == event_id)
            )
            event = event_result.scalar_one_or_none()
            if not event or not event.is_active:
                return

            watches_result = await session.execute(
                select(AvailabilityWatch)
                .where(AvailabilityWatch.event_id == event_id)
                .where(AvailabilityWatch.is_active == True)
            )
            watches = watches_result.scalars().all()
            if not watches:
                return

            scraped_results, _ = await asyncio.get_running_loop().run_in_executor(
                None, scrape_event_sync, event.ticketmaster_url
            )

            scraped_map: dict[tuple[str, int], float] = {}
            for r in scraped_results:
                if r.quantity is not None:
                    key = (r.name.strip().lower(), r.quantity)
                    if key not in scraped_map or r.min_price < scraped_map[key]:
                        scraped_map[key] = r.min_price

            now = datetime.now(timezone.utc)

            for watch in watches:
                key = (watch.section_name.strip().lower(), watch.quantity)
                if key not in scraped_map:
                    continue

                current_price = scraped_map[key]

                if watch.target_price is not None:
                    if current_price > watch.target_price:
                        continue

                if watch.last_alerted_at is not None:
                    cooldown_cutoff = now - timedelta(
                        minutes=watch.alert_cooldown_minutes
                    )
                    if watch.last_alerted_at > cooldown_cutoff:
                        continue

                await fire_availability_alert(watch, event, current_price)
                watch.last_alerted_at = now

            await session.commit()

        except Exception as e:
            logger.error(
                "run_availability_check(%d) failed: %s", event_id, e,
                exc_info=True,
            )


async def fire_availability_alert(
    watch: AvailabilityWatch, event: Event, price: float
) -> None:
    from app.notifier import get_notifier_manager
    manager = get_notifier_manager()

    if watch.target_price is not None:
        price_line = (
            f"<b>Price:</b> ${price:.2f} "
            f"(your target: ${watch.target_price:.2f})\n"
        )
    else:
        price_line = f"<b>Price:</b> ${price:.2f}\n"

    message = (
        f"🎟 <b>Section Now Available</b>\n\n"
        f"<b>Event:</b> {event.name}\n"
        f"<b>Section:</b> {watch.section_name}\n"
        f"<b>Quantity:</b> {watch.quantity} ticket"
        f"{'s' if watch.quantity != 1 else ''}\n"
        f"{price_line}"
        f"\n<a href='{event.ticketmaster_url}'>View on Ticketmaster</a>"
    )
    await manager.send_all(message)
    logger.info(
        "Availability alert sent for %s × %d @ $%.2f",
        watch.section_name, watch.quantity, price,
    )


async def run_watch_job(watch_id: int) -> None:
    logger.info("run_watch_job started")
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
            loop = asyncio.get_event_loop()
            scraped_results, _ = await loop.run_in_executor(
                None, scrape_event_sync, event.ticketmaster_url
            )
        except Exception as e:
            logger.error("Scrape failed for watch %d: %s", watch_id, e)
            return

        now = datetime.now(timezone.utc)

        # Step 6: Find the matching listing by name and quantity
        matched_result = None
        for r in scraped_results:
            if (r.name.strip().lower() == listing.name.strip().lower()
                    and r.quantity == listing.quantity):
                matched_result = r
                break

        # Step 7: Reconcile all listings for this event
        all_db_result = await session.execute(
            select(Listing).where(Listing.event_id == event.id)
        )
        all_db_listings = all_db_result.scalars().all()
        known_by_key = {(l.name.strip().lower(), l.quantity): l for l in all_db_listings}

        scraped_keys = {(r.name.strip().lower(), r.quantity) for r in scraped_results}

        for r in scraped_results:
            key = (r.name.strip().lower(), r.quantity)
            if key in known_by_key:
                known_by_key[key].last_seen_at = now
                known_by_key[key].is_available = True
            else:
                session.add(Listing(
                    event_id=event.id,
                    name=r.name.strip(),
                    quantity=r.quantity,
                    is_available=True,
                    last_seen_at=now,
                ))

        for db_listing in all_db_listings:
            key = (db_listing.name.strip().lower(), db_listing.quantity)
            if key not in scraped_keys:
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
            if current_price <= watch.target_price:
                cooldown_cutoff = now - timedelta(minutes=watch.alert_cooldown_minutes)
                recent_result = await session.execute(
                    select(AlertLog)
                    .where(AlertLog.listing_id == listing.id)
                    .where(AlertLog.alerted_at > cooldown_cutoff)
                    .limit(1)
                )
                if not recent_result.scalar_one_or_none():
                    channels_used = await fire_alert(watch, listing, event, current_price)
                    session.add(AlertLog(
                        listing_id=listing.id,
                        price_at_alert=current_price,
                        alerted_at=now,
                        channels_used=channels_used,
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


async def fire_alert(watch, listing, event, price: float) -> str:
    from app.notifier import get_notifier_manager
    manager = get_notifier_manager()

    message = (
        f"🎟 <b>Price Drop Alert</b>\n\n"
        f"<b>Event:</b> {event.name}\n"
        f"<b>Section:</b> {listing.name}\n"
        f"<b>Price:</b> ${price:.2f}\n"
        f"<b>Your target:</b> ${watch.target_price:.2f}\n\n"
        f"<a href='{event.ticketmaster_url}'>View on Ticketmaster</a>"
    )

    channels = manager.channel_names
    await manager.send_all(message)
    logger.info("Alert sent via %s for %s @ $%.2f", channels, listing.name, price)
    return ",".join(channels) if channels else "none"

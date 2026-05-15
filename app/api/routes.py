import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import AlertLog, Event, Listing, PriceSnapshot, UserWatch
from app.scraper.ticketmaster import scrape_event as _scrape, scrape_event_sync as _scrape_sync
from app.scheduler.engine import remove_watch_job, schedule_watch_job, scheduler

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_quantities(qty_str: str | None) -> list[int]:
    """Parse comma-separated quantity string into sorted int list. Falls back to [1..6]."""
    if not qty_str:
        return [1, 2, 3, 4, 5, 6]
    try:
        result = sorted({int(q) for q in qty_str.split(",") if q.strip()})
        return result if result else [1, 2, 3, 4, 5, 6]
    except ValueError:
        return [1, 2, 3, 4, 5, 6]


def _parse_url_slug(url: str) -> tuple[str, str | None]:
    """Returns (event_name, event_date_str) parsed from a Ticketmaster URL slug."""
    parsed = urlparse(url)
    segments = [s for s in parsed.path.strip("/").split("/") if s]

    slug = max(
        (s for s in segments if "-" in s and s[0].isalpha()),
        key=len,
        default="",
    )
    if not slug:
        return "Unknown Event", None

    parts = slug.split("-")

    # Detect MM-DD-YYYY date pattern anywhere in the slug
    date_str = None
    date_start = None
    for i in range(len(parts) - 2):
        if (
            re.match(r"^\d{1,2}$", parts[i])
            and re.match(r"^\d{1,2}$", parts[i + 1])
            and re.match(r"^\d{4}$", parts[i + 2])
        ):
            date_str = f"{parts[i]}-{parts[i + 1]}-{parts[i + 2]}"
            date_start = i
            break

    name_parts = parts[:date_start] if date_start is not None else parts
    name = " ".join(w.capitalize() for w in name_parts if w)
    return name or "Unknown Event", date_str


async def _reconcile_listings(
    db: AsyncSession, event_id: int, scraped_results: list
) -> None:
    """Upsert scraped listings into the DB, marking missing ones unavailable."""
    now = datetime.now(timezone.utc)
    all_result = await db.execute(select(Listing).where(Listing.event_id == event_id))
    db_listings = all_result.scalars().all()
    known = {l.name.strip().lower(): l for l in db_listings}
    scraped_names = {r.name.strip().lower() for r in scraped_results}

    for r in scraped_results:
        key = r.name.strip().lower()
        if key in known:
            known[key].last_seen_at = now
            known[key].is_available = True
        else:
            db.add(Listing(event_id=event_id, name=r.name.strip(), is_available=True, last_seen_at=now))

    for db_listing in db_listings:
        if db_listing.name.strip().lower() not in scraped_names:
            db_listing.is_available = False


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    events_result = await db.execute(select(Event).order_by(desc(Event.added_at)))
    events = events_result.scalars().all()
    now_utc = datetime.now(timezone.utc)
    alert_cutoff = now_utc - timedelta(hours=24)

    _STATUS_PRIORITY = {"Near Target": 0, "Price Dropping": 1, "Price Rising": 2, "Stable": 3}

    events_list = []
    for event in events:
        # All active watches (with their listings) for this event
        watches_result = await db.execute(
            select(UserWatch, Listing)
            .join(Listing, UserWatch.listing_id == Listing.id)
            .where(Listing.event_id == event.id)
            .where(UserWatch.is_active == True)
        )
        watch_rows = watches_result.all()

        lowest_price = None
        last_checked_mins_ago = None
        refresh_interval_minutes = None
        target_price = None
        status = "Stable"

        # Most recent snapshot across all listings — drives "Last checked"
        latest_snap_result = await db.execute(
            select(PriceSnapshot)
            .join(Listing, PriceSnapshot.listing_id == Listing.id)
            .where(Listing.event_id == event.id)
            .order_by(desc(PriceSnapshot.scraped_at))
            .limit(1)
        )
        latest_snap = latest_snap_result.scalar_one_or_none()
        if latest_snap:
            scraped_at = latest_snap.scraped_at
            if scraped_at.tzinfo is None:
                scraped_at = scraped_at.replace(tzinfo=timezone.utc)
            last_checked_mins_ago = max(0, int((now_utc - scraped_at).total_seconds() / 60))

        if watch_rows:
            refresh_interval_minutes = watch_rows[0][0].refresh_interval_minutes
            target_price = watch_rows[0][0].target_price
            watched_listing_ids = [row[1].id for row in watch_rows]

            # Check for an alert fired in the last 24 h
            recent_alert_result = await db.execute(
                select(AlertLog)
                .where(AlertLog.listing_id.in_(watched_listing_ids))
                .where(AlertLog.alerted_at >= alert_cutoff)
                .limit(1)
            )
            recent_alert = recent_alert_result.scalar_one_or_none()

            per_watch_statuses = []
            for watch, listing in watch_rows:
                snaps_result = await db.execute(
                    select(PriceSnapshot)
                    .where(PriceSnapshot.listing_id == listing.id)
                    .order_by(desc(PriceSnapshot.scraped_at))
                    .limit(2)
                )
                snaps = snaps_result.scalars().all()
                if snaps:
                    latest_price = snaps[0].price
                    if lowest_price is None or latest_price < lowest_price:
                        lowest_price = latest_price
                    if len(snaps) >= 2:
                        prev_price = snaps[1].price
                        if latest_price > watch.target_price and latest_price <= watch.target_price * 1.10:
                            per_watch_statuses.append("Near Target")
                        elif latest_price < prev_price:
                            per_watch_statuses.append("Price Dropping")
                        elif latest_price > prev_price:
                            per_watch_statuses.append("Price Rising")
                        else:
                            per_watch_statuses.append("Stable")

            if per_watch_statuses:
                status = min(per_watch_statuses, key=lambda s: _STATUS_PRIORITY.get(s, 99))
            if recent_alert:
                status = "Alert Sent"
        else:
            # No watches — fall back to all listings for lowest price
            all_ids_result = await db.execute(
                select(Listing.id).where(Listing.event_id == event.id)
            )
            for lid in all_ids_result.scalars().all():
                snap_result = await db.execute(
                    select(PriceSnapshot)
                    .where(PriceSnapshot.listing_id == lid)
                    .order_by(desc(PriceSnapshot.scraped_at))
                    .limit(1)
                )
                snap = snap_result.scalar_one_or_none()
                if snap and (lowest_price is None or snap.price < lowest_price):
                    lowest_price = snap.price

        # Pre-compute sortable date (YYYY-MM-DD) and % to target for client-side sorting
        sort_date = "9999-99-99"
        if event.event_date:
            try:
                m, d, y = event.event_date.split("-")
                sort_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
            except Exception:
                pass

        pct_to_target = 9999
        if lowest_price is not None and target_price is not None and target_price > 0:
            pct_to_target = round((lowest_price / target_price - 1) * 100, 1)

        events_list.append({
            "id": event.id,
            "name": event.name,
            "venue": event.venue,
            "event_date": event.event_date,
            "is_active": event.is_active,
            "lowest_price": lowest_price,
            "target_price": target_price,
            "status": status,
            "last_checked_mins_ago": last_checked_mins_ago,
            "refresh_interval_minutes": refresh_interval_minutes,
            "sort_date": sort_date,
            "pct_to_target": pct_to_target,
        })

    return templates.TemplateResponse(request, "dashboard.html", {"events": events_list})


@router.get("/events/{event_id}/setup", response_class=HTMLResponse)
async def event_setup(event_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event_obj = event_result.scalar_one_or_none()
    if not event_obj:
        raise HTTPException(status_code=404, detail="Event not found")

    available_quantities = _parse_quantities(event_obj.available_quantities)
    return templates.TemplateResponse(request, "setup.html", {
        "event": {
            "id": event_obj.id,
            "name": event_obj.name,
            "venue": event_obj.venue,
            "event_date": event_obj.event_date,
        },
        "available_quantities": available_quantities,
    })


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail(event_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event_obj = event_result.scalar_one_or_none()
    if not event_obj:
        raise HTTPException(status_code=404, detail="Event not found")

    if event_obj.quantity is None:
        return RedirectResponse(url=f"/events/{event_id}/setup", status_code=303)

    listings_result = await db.execute(
        select(Listing).where(Listing.event_id == event_id)
    )
    listings = listings_result.scalars().all()

    listings_data = []
    snapshot_chart_data = []

    for listing in listings:
        watch_result = await db.execute(
            select(UserWatch).where(UserWatch.listing_id == listing.id).limit(1)
        )
        watch = watch_result.scalar_one_or_none()

        snaps_result = await db.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.listing_id == listing.id)
            .order_by(PriceSnapshot.scraped_at.asc())
            .limit(50)
        )
        snapshots = snaps_result.scalars().all()
        snaps_dicts = [{"price": s.price, "scraped_at": s.scraped_at.isoformat()} for s in snapshots]

        if snapshots:  # only show listings with price data for selected quantity
            listings_data.append({
                "id": listing.id,
                "name": listing.name,
                "is_available": listing.is_available,
                "current_price": snapshots[-1].price,
                "watch": {
                    "id": watch.id,
                    "target_price": watch.target_price,
                    "refresh_interval_minutes": watch.refresh_interval_minutes,
                    "alert_cooldown_minutes": watch.alert_cooldown_minutes,
                } if watch else None,
                "snapshots": snaps_dicts,
                "snapshot_count": len(snapshots),
            })

        if len(snapshots) >= 2:
            snapshot_chart_data.append({
                "listing_id": listing.id,
                "name": listing.name,
                "snapshots": snaps_dicts,
            })

    available_quantities = _parse_quantities(event_obj.available_quantities)
    return templates.TemplateResponse(request, "event.html", {
        "event": {
            "id": event_obj.id,
            "name": event_obj.name,
            "venue": event_obj.venue,
            "event_date": event_obj.event_date,
            "ticketmaster_url": event_obj.ticketmaster_url,
            "is_active": event_obj.is_active,
            "quantity": event_obj.quantity,
        },
        "available_quantities": available_quantities,
        "listings": listings_data,
        "snapshot_chart_data": snapshot_chart_data,
        "ai_enabled": bool(get_settings().ai_api_key),
    })


# ---------------------------------------------------------------------------
# Event API
# ---------------------------------------------------------------------------


@router.post("/events/add")
async def add_event(
    request: Request,
    url: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if "ticketmaster" not in url.lower():
        return JSONResponse(status_code=400, content={"error": "URL must be a Ticketmaster URL"})

    existing_result = await db.execute(select(Event).where(Event.ticketmaster_url == url))
    if existing_result.scalar_one_or_none():
        return RedirectResponse(url="/", status_code=303)

    try:
        loop = asyncio.get_event_loop()
        # quantity=None → unfiltered scrape; discovers all listings and available quantities
        scraped_results, available_quantities = await loop.run_in_executor(None, _scrape_sync, url)
    except Exception as e:
        import traceback
        logger.error(f"Add event failed: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Scrape failed: {type(e).__name__}: {e}"}
        )

    if not scraped_results:
        return JSONResponse(status_code=400, content={"error": "No listings found at that URL"})

    name, event_date = _parse_url_slug(url)
    now = datetime.now(timezone.utc)

    event = Event(
        name=name,
        venue=None,
        event_date=event_date,
        ticketmaster_url=url,
        available_quantities=",".join(str(q) for q in available_quantities),
        quantity=None,
        added_at=now,
        is_active=True,
    )
    db.add(event)
    await db.flush()

    for r in scraped_results:
        db.add(Listing(
            event_id=event.id,
            name=r.name.strip(),
            is_available=True,
            last_seen_at=now,
        ))

    await db.flush()

    listings_result = await db.execute(
        select(Listing).where(Listing.event_id == event.id)
    )
    saved_listings = listings_result.scalars().all()
    listing_map = {l.name.strip().lower(): l.id for l in saved_listings}

    for r in scraped_results:
        lid = listing_map.get(r.name.strip().lower())
        if lid:
            db.add(PriceSnapshot(
                listing_id=lid,
                price=r.min_price,
                scraped_at=now,
            ))

    await db.commit()
    return RedirectResponse(url=f"/events/{event.id}/setup", status_code=303)


@router.post("/events/{event_id}/quantity")
async def update_quantity(
    event_id: int,
    quantity: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    valid_quantities = _parse_quantities(event.available_quantities)
    if quantity not in valid_quantities:
        return JSONResponse(
            status_code=400,
            content={"error": f"Quantity {quantity} is not available for this event"},
        )

    event.quantity = quantity
    await db.flush()

    # Re-scrape filtered by the chosen quantity so stored prices reflect real availability
    try:
        loop = asyncio.get_event_loop()
        scraped_results, _ = await loop.run_in_executor(None, _scrape_sync, event.ticketmaster_url, quantity)
    except Exception as e:
        logger.error("Quantity scrape failed for event %d: %s", event_id, e)
        scraped_results = []

    if scraped_results:
        now = datetime.now(timezone.utc)
        listings_result = await db.execute(select(Listing).where(Listing.event_id == event_id))
        all_listings = listings_result.scalars().all()
        listing_map = {l.name.strip().lower(): l.id for l in all_listings}

        # Get all listing IDs for this event
        all_listing_ids_result = await db.execute(
            select(Listing.id).where(Listing.event_id == event_id)
        )
        all_listing_ids = all_listing_ids_result.scalars().all()

        # Delete old snapshots so stale unfiltered prices don't show
        if all_listing_ids:
            from sqlalchemy import delete
            await db.execute(
                delete(PriceSnapshot).where(PriceSnapshot.listing_id.in_(all_listing_ids))
            )

        logger.info(f"Quantity set to {quantity} for event {event_id} — deleted old snapshots, saving {len(scraped_results)} new ones")

        for r in scraped_results:
            lid = listing_map.get(r.name.strip().lower())
            if lid and r.min_price > 0:
                db.add(PriceSnapshot(
                    listing_id=lid,
                    price=r.min_price,
                    scraped_at=now,
                ))

    await db.commit()
    return RedirectResponse(url=f"/events/{event_id}", status_code=303)


@router.post("/events/{event_id}/toggle")
async def toggle_event(event_id: int, db: AsyncSession = Depends(get_db)):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    event.is_active = not event.is_active

    watches_result = await db.execute(
        select(UserWatch)
        .join(Listing, UserWatch.listing_id == Listing.id)
        .where(Listing.event_id == event_id)
        .where(UserWatch.is_active == True)
    )
    watches = watches_result.scalars().all()

    if not event.is_active:
        for watch in watches:
            remove_watch_job(watch.id)
    else:
        for watch in watches:
            schedule_watch_job(watch)

    await db.commit()
    return JSONResponse({"is_active": event.is_active})


@router.delete("/events/{event_id}")
async def delete_event(event_id: int, db: AsyncSession = Depends(get_db)):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    listings_result = await db.execute(
        select(Listing).where(Listing.event_id == event_id)
    )
    listings = listings_result.scalars().all()
    listing_ids = [l.id for l in listings]

    if listing_ids:
        watches_result = await db.execute(
            select(UserWatch).where(UserWatch.listing_id.in_(listing_ids))
        )
        for watch in watches_result.scalars().all():
            remove_watch_job(watch.id)

        await db.execute(delete(AlertLog).where(AlertLog.listing_id.in_(listing_ids)))
        await db.execute(delete(PriceSnapshot).where(PriceSnapshot.listing_id.in_(listing_ids)))
        await db.execute(delete(UserWatch).where(UserWatch.listing_id.in_(listing_ids)))
        await db.execute(delete(Listing).where(Listing.event_id == event_id))

    await db.delete(event)
    await db.commit()
    return JSONResponse({"deleted": True})


@router.get("/events/{event_id}/scrape")
async def trigger_scrape(event_id: int, db: AsyncSession = Depends(get_db)):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    try:
        loop = asyncio.get_event_loop()
        scraped_results, _ = await loop.run_in_executor(
            None, _scrape_sync, event.ticketmaster_url, event.quantity
        )
    except Exception as e:
        logger.error("Manual scrape failed for event %d: %s", event_id, e)
        return JSONResponse(status_code=500, content={"error": f"Scrape failed: {e}"})

    await _reconcile_listings(db, event_id, scraped_results)

    listings_result = await db.execute(
        select(Listing).where(Listing.event_id == event_id)
    )
    all_listings = listings_result.scalars().all()
    listing_map = {l.name.strip().lower(): l.id for l in all_listings}

    for r in scraped_results:
        lid = listing_map.get(r.name.strip().lower())
        if lid and r.min_price > 0:
            db.add(PriceSnapshot(
                listing_id=lid,
                price=r.min_price,
                scraped_at=datetime.now(timezone.utc),
            ))
    await db.commit()

    return JSONResponse({
        "listings_found": len(scraped_results),
        "listings": [{"name": r.name, "price": r.min_price} for r in scraped_results],
    })


# ---------------------------------------------------------------------------
# Listing / Watch API
# ---------------------------------------------------------------------------


@router.post("/listings/{listing_id}/watch")
async def set_watch(
    listing_id: int,
    target_price: float = Form(...),
    refresh_interval_minutes: int = Form(...),
    alert_cooldown_minutes: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    listing_result = await db.execute(select(Listing).where(Listing.id == listing_id))
    if not listing_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Listing not found")

    existing_result = await db.execute(
        select(UserWatch).where(UserWatch.listing_id == listing_id).limit(1)
    )
    watch = existing_result.scalar_one_or_none()
    created = watch is None

    if watch:
        watch.target_price = target_price
        watch.refresh_interval_minutes = refresh_interval_minutes
        watch.alert_cooldown_minutes = alert_cooldown_minutes
        watch.is_active = True
    else:
        watch = UserWatch(
            listing_id=listing_id,
            target_price=target_price,
            refresh_interval_minutes=refresh_interval_minutes,
            alert_cooldown_minutes=alert_cooldown_minutes,
            is_active=True,
        )
        db.add(watch)
        await db.flush()

    schedule_watch_job(watch)
    await db.commit()
    return JSONResponse({"watch_id": watch.id, "created": created})


@router.delete("/listings/{listing_id}/watch")
async def delete_watch(listing_id: int, db: AsyncSession = Depends(get_db)):
    watch_result = await db.execute(
        select(UserWatch).where(UserWatch.listing_id == listing_id).limit(1)
    )
    watch = watch_result.scalar_one_or_none()
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    remove_watch_job(watch.id)
    await db.delete(watch)
    await db.commit()
    return JSONResponse({"deleted": True})


@router.get("/listings/{listing_id}/history")
async def price_history(listing_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.listing_id == listing_id)
        .order_by(PriceSnapshot.scraped_at.asc())
        .limit(100)
    )
    snapshots = result.scalars().all()
    return JSONResponse({
        "snapshots": [
            {"price": s.price, "scraped_at": s.scraped_at.isoformat()}
            for s in snapshots
        ]
    })


@router.post("/listings/{listing_id}/summarize")
async def summarize_listing(listing_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.listing_id == listing_id)
        .order_by(PriceSnapshot.scraped_at.asc())
        .limit(50)
    )
    snapshots = result.scalars().all()
    snaps_dicts = [{"price": s.price, "scraped_at": s.scraped_at.isoformat()} for s in snapshots]
    from app.ai.client import summarize_price_history
    summary = await summarize_price_history(snaps_dicts)
    return JSONResponse({"summary": summary})


# ---------------------------------------------------------------------------
# Search + Health
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str


@router.post("/events/search")
async def search_events(body: SearchRequest):
    settings = get_settings()
    if not settings.ai_api_key:
        return JSONResponse({"error": "AI not configured", "ai_parsed": False})

    from app.ai.client import parse_event_search
    result = await parse_event_search(body.query)
    if result is None:
        return JSONResponse({"error": "AI call failed", "ai_parsed": False})
    return JSONResponse({"results": result, "ai_parsed": True})


@router.get("/api/health")
async def health():
    return JSONResponse({"status": "ok", "scheduler_running": scheduler.running})

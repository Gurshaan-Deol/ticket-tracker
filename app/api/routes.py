import asyncio
import json
import logging
import re
from datetime import datetime, timezone
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

    events_list = []
    for event in events:
        # Try watched listings first
        watched_ids_result = await db.execute(
            select(Listing.id)
            .join(UserWatch, UserWatch.listing_id == Listing.id)
            .where(Listing.event_id == event.id)
            .where(UserWatch.is_active == True)
        )
        watched_listing_ids = watched_ids_result.scalars().all()

        lowest_price = None

        # Check watched listings for latest snapshots
        ids_to_check = watched_listing_ids if watched_listing_ids else []

        # If no watched listings, fall back to all listings
        if not ids_to_check:
            all_ids_result = await db.execute(
                select(Listing.id).where(Listing.event_id == event.id)
            )
            ids_to_check = all_ids_result.scalars().all()

        for lid in ids_to_check:
            snap_result = await db.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.listing_id == lid)
                .order_by(desc(PriceSnapshot.scraped_at))
                .limit(1)
            )
            snap = snap_result.scalar_one_or_none()
            if snap and (lowest_price is None or snap.price < lowest_price):
                lowest_price = snap.price

        events_list.append({
            "id": event.id,
            "name": event.name,
            "venue": event.venue,
            "event_date": event.event_date,
            "is_active": event.is_active,
            "lowest_price": lowest_price,
        })

    return templates.TemplateResponse(request, "dashboard.html", {"events": events_list})


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail(event_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event_obj = event_result.scalar_one_or_none()
    if not event_obj:
        raise HTTPException(status_code=404, detail="Event not found")

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

        listings_data.append({
            "id": listing.id,
            "name": listing.name,
            "is_available": listing.is_available,
            "current_price": snapshots[-1].price if snapshots else None,
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

    return templates.TemplateResponse(request, "event.html", {
        "event": {
            "id": event_obj.id,
            "name": event_obj.name,
            "venue": event_obj.venue,
            "event_date": event_obj.event_date,
            "ticketmaster_url": event_obj.ticketmaster_url,
            "is_active": event_obj.is_active,
        },
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
        scraped_results, _ = await loop.run_in_executor(None, _scrape_sync, url)
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

    # Save initial price snapshots for all listings
    await db.flush()  # ensure all listing IDs are available

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
    return RedirectResponse(url=f"/events/{event.id}", status_code=303)


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
        scraped_results, _ = await loop.run_in_executor(None, _scrape_sync, event.ticketmaster_url)
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

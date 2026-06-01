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
from sqlalchemy import and_, delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import AlertHistoryLog, AlertLog, AvailabilityWatch, Event, Listing, PriceSnapshot, UserWatch, VenueSection
from app.scraper.ticketmaster import EventEndedException, SoldOutException, fetch_venue_sections, scrape_event_sync as _scrape_sync
from app.scheduler.engine import (
    _cancel_all_jobs_for_event,
    remove_availability_job,
    remove_watch_job,
    schedule_availability_job,
    schedule_watch_job,
    scheduler,
)

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


GA_GROUP_KEYWORDS = ["LAWN", "GA", "GENERAL ADMISSION"]


def _matches_ga_keyword(name: str, keyword: str) -> bool:
    n = name.strip().upper()
    k = keyword.upper()
    if n == k:
        return True
    if n.startswith(k) and len(n) > len(k):
        next_char = n[len(k)]
        return next_char == " " or next_char.isdigit()
    return False


async def _reconcile_listings(
    db: AsyncSession, event_id: int, scraped_results: list
) -> None:
    """Upsert scraped listings into the DB, marking missing ones unavailable."""
    now = datetime.now(timezone.utc)
    all_result = await db.execute(select(Listing).where(Listing.event_id == event_id))
    db_listings = all_result.scalars().all()
    known = {
        (l.name.strip().lower(), l.quantity): l
        for l in db_listings
    }
    scraped_keys = {
        (r.name.strip().lower(), r.quantity)
        for r in scraped_results
    }

    for r in scraped_results:
        key = (r.name.strip().lower(), r.quantity)
        if key in known:
            known[key].last_seen_at = now
            known[key].is_available = True
        else:
            db.add(Listing(
                event_id=event_id,
                name=r.name.strip(),
                quantity=r.quantity,
                is_available=True,
                last_seen_at=now,
            ))

    for db_listing in db_listings:
        key = (db_listing.name.strip().lower(), db_listing.quantity)
        if key not in scraped_keys:
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

    event_ids = [e.id for e in events]
    if not event_ids:
        return templates.TemplateResponse(request, "dashboard.html", {"events": []})

    # Bulk 1: all active (watch, listing) pairs across all events — replaces per-event query
    _all_watch_rows = await db.execute(
        select(UserWatch, Listing)
        .join(Listing, UserWatch.listing_id == Listing.id)
        .where(Listing.event_id.in_(event_ids))
        .where(UserWatch.is_active == True)
    )
    watch_rows_by_event: dict[int, list] = {}
    watched_listing_ids_by_event: dict[int, list[int]] = {}
    _all_watched_listing_ids: list[int] = []
    for _w, _l in _all_watch_rows.all():
        watch_rows_by_event.setdefault(_l.event_id, []).append((_w, _l))
        watched_listing_ids_by_event.setdefault(_l.event_id, []).append(_l.id)
        _all_watched_listing_ids.append(_l.id)

    # Bulk 2: most recent scraped_at per event (drives "last checked" display)
    _latest_snap_subq = (
        select(
            Listing.event_id,
            func.max(PriceSnapshot.scraped_at).label("latest_at"),
        )
        .select_from(PriceSnapshot)
        .join(Listing, PriceSnapshot.listing_id == Listing.id)
        .where(Listing.event_id.in_(event_ids))
        .group_by(Listing.event_id)
        .subquery()
    )
    latest_at_by_event: dict[int, datetime] = {}
    for row in (await db.execute(
        select(_latest_snap_subq.c.event_id, _latest_snap_subq.c.latest_at)
    )).all():
        latest_at_by_event[row.event_id] = row.latest_at

    # Bulk 3: lowest current available price per event
    _latest_per_listing_subq = (
        select(
            PriceSnapshot.listing_id,
            func.max(PriceSnapshot.scraped_at).label("latest_at"),
        )
        .group_by(PriceSnapshot.listing_id)
        .subquery()
    )
    lowest_price_by_event: dict[int, float] = {}
    for row in (await db.execute(
        select(Listing.event_id, func.min(PriceSnapshot.price).label("min_price"))
        .join(_latest_per_listing_subq, and_(
            PriceSnapshot.listing_id == _latest_per_listing_subq.c.listing_id,
            PriceSnapshot.scraped_at == _latest_per_listing_subq.c.latest_at,
        ))
        .join(Listing, PriceSnapshot.listing_id == Listing.id)
        .where(Listing.event_id.in_(event_ids))
        .where(Listing.is_available == True)
        .group_by(Listing.event_id)
    )).all():
        lowest_price_by_event[row.event_id] = row.min_price

    # Bulk 4: latest 2 snapshots per watched listing — replaces the per-watch inner loop query
    snap_prices_by_listing: dict[int, list[float]] = {}
    if _all_watched_listing_ids:
        _ranked_snaps_subq = (
            select(
                PriceSnapshot.listing_id,
                PriceSnapshot.price,
                func.row_number().over(
                    partition_by=PriceSnapshot.listing_id,
                    order_by=desc(PriceSnapshot.scraped_at),
                ).label("rn"),
            )
            .where(PriceSnapshot.listing_id.in_(_all_watched_listing_ids))
            .subquery()
        )
        for row in (await db.execute(
            select(_ranked_snaps_subq.c.listing_id, _ranked_snaps_subq.c.price)
            .where(_ranked_snaps_subq.c.rn <= 2)
            .order_by(_ranked_snaps_subq.c.listing_id, _ranked_snaps_subq.c.rn)
        )).all():
            snap_prices_by_listing.setdefault(row.listing_id, []).append(row.price)

    # Bulk 5: any alert fired in last 24 h, keyed by listing_id
    recent_alerted_listing_ids: set[int] = set()
    if _all_watched_listing_ids:
        for row in (await db.execute(
            select(AlertLog.listing_id)
            .where(AlertLog.listing_id.in_(_all_watched_listing_ids))
            .where(AlertLog.alerted_at >= alert_cutoff)
            .distinct()
        )).all():
            recent_alerted_listing_ids.add(row.listing_id)

    logger.debug(
        "Dashboard: 6 fixed queries for %d events, %d watched listings",
        len(events), len(_all_watched_listing_ids),
    )

    events_list = []
    for event in events:
        watch_rows = watch_rows_by_event.get(event.id, [])

        setup_complete = event.quantity is not None
        lowest_price = None
        last_checked_mins_ago = None
        refresh_interval_minutes = None
        target_price = None
        status = "Stable"

        if setup_complete:
            latest_at = latest_at_by_event.get(event.id)
            if latest_at:
                if latest_at.tzinfo is None:
                    latest_at = latest_at.replace(tzinfo=timezone.utc)
                last_checked_mins_ago = max(0, int((now_utc - latest_at).total_seconds() / 60))

            lowest_price = lowest_price_by_event.get(event.id)

            if watch_rows:
                refresh_interval_minutes = watch_rows[0][0].refresh_interval_minutes
                target_price = watch_rows[0][0].target_price
                event_listing_ids = watched_listing_ids_by_event.get(event.id, [])

                recent_alert = any(lid in recent_alerted_listing_ids for lid in event_listing_ids)

                per_watch_statuses = []
                for watch, listing in watch_rows:
                    snaps = snap_prices_by_listing.get(listing.id, [])
                    if snaps:
                        latest_price = snaps[0]
                        if len(snaps) >= 2:
                            prev_price = snaps[1]
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
            "ticketmaster_url": event.ticketmaster_url,
            "is_active": event.is_active,
            "is_ended": event.is_ended,
            "setup_complete": setup_complete,
            "lowest_price": lowest_price,
            "target_price": target_price,
            "status": status,
            "last_checked_mins_ago": last_checked_mins_ago,
            "refresh_interval_minutes": refresh_interval_minutes,
            "sort_date": sort_date,
            "pct_to_target": pct_to_target,
            "date_changed_at": event.date_changed_at,
            "previous_event_date": event.previous_event_date,
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

    listing_ids = [l.id for l in listings]

    # Bulk 1: all UserWatch rows for this event's listings — replaces per-listing query
    watches_by_listing: dict[int, list] = {}
    if listing_ids:
        for w in (await db.execute(
            select(UserWatch).where(UserWatch.listing_id.in_(listing_ids))
        )).scalars().all():
            watches_by_listing.setdefault(w.listing_id, []).append(w)

    # Bulk 2: earliest 50 snapshots per listing via window function — replaces per-listing query
    snaps_by_listing: dict[int, list[dict]] = {}
    if listing_ids:
        _snap_ranked_subq = (
            select(
                PriceSnapshot.listing_id,
                PriceSnapshot.price,
                PriceSnapshot.scraped_at,
                func.row_number().over(
                    partition_by=PriceSnapshot.listing_id,
                    order_by=PriceSnapshot.scraped_at.asc(),
                ).label("rn"),
            )
            .where(PriceSnapshot.listing_id.in_(listing_ids))
            .subquery()
        )
        for row in (await db.execute(
            select(
                _snap_ranked_subq.c.listing_id,
                _snap_ranked_subq.c.price,
                _snap_ranked_subq.c.scraped_at,
            )
            .where(_snap_ranked_subq.c.rn <= 50)
            .order_by(_snap_ranked_subq.c.listing_id, _snap_ranked_subq.c.scraped_at)
        )).all():
            snaps_by_listing.setdefault(row.listing_id, []).append(
                {"price": row.price, "scraped_at": row.scraped_at.isoformat()}
            )

    logger.debug(
        "Event detail %d: 4 fixed queries for %d listings",
        event_id, len(listing_ids),
    )

    listings_data = []
    snapshot_chart_data = []

    for listing in listings:
        watches = watches_by_listing.get(listing.id, [])
        snaps_dicts = snaps_by_listing.get(listing.id, [])

        if snaps_dicts:
            listings_data.append({
                "id": listing.id,
                "name": listing.name,
                "quantity": listing.quantity,
                "is_available": listing.is_available,
                "is_grouped": False,
                "last_seen_at": listing.last_seen_at,
                "current_price": snaps_dicts[-1]["price"],
                "watches": [
                    {
                        "id": w.id,
                        "target_price": w.target_price,
                        "refresh_interval_minutes": w.refresh_interval_minutes,
                        "alert_cooldown_minutes": w.alert_cooldown_minutes,
                        "quantity": w.quantity,
                    }
                    for w in watches
                ],
                "snapshots": snaps_dicts,
                "snapshot_count": len(snaps_dicts),
            })

        if len(snaps_dicts) >= 2:
            snapshot_chart_data.append({
                "listing_id": listing.id,
                "name": listing.name,
                "snapshots": snaps_dicts,
            })

    logger.info(f"listings_data: {len(listings_data)} total, "
                f"{sum(1 for l in listings_data if not l['is_available'])} unavailable")

    _avail_grouping_used: set = set()
    _grouped_avail: list[dict] = []
    for keyword in GA_GROUP_KEYWORDS:
        matching = [l for l in listings_data if l["is_available"] and _matches_ga_keyword(l["name"], keyword)]
        if len(matching) >= 2:
            qty_price_map: dict[str, float] = {}
            qty_cheapest_id: dict[str, int] = {}
            for l in matching:
                if l["current_price"] is not None and l["quantity"] is not None:
                    k = str(l["quantity"])
                    if k not in qty_price_map or l["current_price"] < qty_price_map[k]:
                        qty_price_map[k] = l["current_price"]
                        if l["id"] is not None:
                            qty_cheapest_id[k] = l["id"]
            lowest = min(qty_price_map.values(), default=None)
            _grouped_avail.append({
                "id": None,
                "name": keyword.title(),
                "quantity": None,
                "is_available": True,
                "is_grouped": True,
                "current_price": lowest,
                "quantity_price_map": qty_price_map,
                "qty_cheapest_id": qty_cheapest_id,
                "constituent_section_names": [l["name"] for l in matching],
                "watches": [],
                "snapshots": [],
                "snapshot_count": 0,
                "last_seen_at": None,
            })
            for l in matching:
                _avail_grouping_used.add(l["id"])
    listings_data = _grouped_avail + [l for l in listings_data if l.get("id") not in _avail_grouping_used]
    already_grouped_as_available = {row["name"].upper() for row in _grouped_avail}

    av_watches_result = await db.execute(
        select(AvailabilityWatch)
        .where(AvailabilityWatch.event_id == event_id)
        .where(AvailabilityWatch.is_active == True)
        .order_by(AvailabilityWatch.section_name, AvailabilityWatch.quantity)
    )
    av_watches = av_watches_result.scalars().all()
    av_watches_data = [
        {
            "id": w.id,
            "section_name": w.section_name,
            "quantity": w.quantity,
            "target_price": w.target_price,
            "alert_cooldown_minutes": w.alert_cooldown_minutes,
            "last_alerted_at": w.last_alerted_at.isoformat()
                               if w.last_alerted_at else None,
        }
        for w in av_watches
    ]

    # All venue sections for this event (from manifest)
    sections_result = await db.execute(
        select(VenueSection)
        .where(VenueSection.event_id == event_id)
        .order_by(VenueSection.name)
    )
    all_sections = sections_result.scalars().all()

    # Currently available listing names (case-insensitive)
    available_result = await db.execute(
        select(Listing.name)
        .where(Listing.event_id == event_id, Listing.is_available == True)
        .distinct()
    )
    available_names = {row[0].strip().lower() for row in available_result.all()}

    # Active listings keyed by (name, qty_str) — needed before unavailable_sections
    avail_result = await db.execute(
        select(Listing.name, Listing.quantity)
        .where(Listing.event_id == event_id, Listing.is_available == True)
    )
    available_name_qty = {
        (row[0].strip().lower(), str(row[1]))
        for row in avail_result.all()
    }
    all_active_qtys = sorted({pair[1] for pair in available_name_qty})

    # A section is unavailable if it lacks an active listing for AT LEAST ONE
    # active quantity — not just absent entirely
    def is_section_unavailable(s):
        key = s.name.strip().lower()
        if key not in available_names:
            return True
        return any(
            (key, q) not in available_name_qty
            for q in all_active_qtys
        )

    unavailable_sections = [
        s for s in all_sections
        if is_section_unavailable(s)
    ]
    # Historically seen quantities per section name from Listing table
    qty_result = await db.execute(
        select(Listing.name, Listing.quantity)
        .where(
            Listing.event_id == event_id,
            Listing.quantity.isnot(None),
        )
        .distinct()
    )
    seen_quantities: dict[str, list[int]] = {}
    for row in qty_result.all():
        key = row[0].strip().lower()
        if key not in seen_quantities:
            seen_quantities[key] = []
        if row[1] not in seen_quantities[key]:
            seen_quantities[key].append(row[1])
    for key in seen_quantities:
        seen_quantities[key].sort()

    # Group GA sections by keyword (2+ matches → single grouped row)
    final_sections: list[dict] = []
    used_ids: set[int] = set()

    for keyword in GA_GROUP_KEYWORDS:
        matching = [s for s in unavailable_sections if _matches_ga_keyword(s.name, keyword)]
        if len(matching) >= 2:
            agg_qtys = sorted({
                qty
                for s in matching
                for qty in seen_quantities.get(s.name.strip().lower(), [])
            })
            agg_seen_str = [str(q) for q in agg_qtys]
            all_relevant_qtys = sorted(
                set(agg_seen_str) | set(all_active_qtys),
                key=lambda x: int(x)
            )
            unavail_qtys = [
                q for q in all_relevant_qtys
                if not any(
                    (s.name.strip().lower(), q) in available_name_qty
                    for s in matching
                )
            ]
            if not unavail_qtys:
                unavail_qtys = ["1", "2", "3", "4", "5", "6"]
            final_sections.append({
                "name": keyword.title(),
                "display_name": keyword.title(),
                "section_names": [s.name for s in matching],
                "is_ga": True,
                "is_grouped": True,
                "count": len(matching),
                "seen_quantities": agg_qtys,
                "unavailable_quantities": unavail_qtys,
            })
            for s in matching:
                used_ids.add(id(s))

    for s in unavailable_sections:
        if id(s) in used_ids:
            continue
        section_key = s.name.strip().lower()
        raw_seen = seen_quantities.get(section_key, [])
        seen_qtys_str = [str(q) for q in raw_seen]
        all_relevant_qtys = sorted(
            set(seen_qtys_str) | set(all_active_qtys),
            key=lambda x: int(x)
        )
        unavail_qtys = [
            q for q in all_relevant_qtys
            if (section_key, q) not in available_name_qty
        ]
        if not unavail_qtys:
            unavail_qtys = ["1", "2", "3", "4", "5", "6"]
        final_sections.append({
            "name": s.name,
            "is_ga": s.is_ga,
            "is_grouped": False,
            "seen_quantities": seen_quantities.get(section_key, []),
            "unavailable_quantities": unavail_qtys,
        })

    unavailable_section_data = final_sections

    available_quantities = _parse_quantities(event_obj.available_quantities)
    return templates.TemplateResponse(request, "event.html", {
        "event": {
            "id": event_obj.id,
            "name": event_obj.name,
            "venue": event_obj.venue,
            "event_date": event_obj.event_date,
            "ticketmaster_url": event_obj.ticketmaster_url,
            "is_active": event_obj.is_active,
            "is_ended": event_obj.is_ended,
            "quantity": event_obj.quantity,
            "notes": event_obj.notes,
        },
        "available_quantities": available_quantities,
        "listings": listings_data,
        "snapshot_chart_data": snapshot_chart_data,
        "ai_enabled": bool(get_settings().ai_api_key),
        "availability_watches": av_watches_data,
        "unavailable_section_data": unavailable_section_data,
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

    is_sold_out = False
    scraped_date: str | None = None
    try:
        loop = asyncio.get_event_loop()
        # quantity=None → unfiltered scrape; discovers all listings and available quantities
        scraped_results, available_quantities, scraped_date = await loop.run_in_executor(None, _scrape_sync, url)
    except EventEndedException:
        # Event is already over — Ticketmaster is no longer serving listings for it.
        # The Event row has not been created yet at this point, so nothing to clean up.
        return JSONResponse(
            status_code=400,
            content={"error": "This event has already ended. Ticketmaster is no longer showing tickets for it."},
        )
    except SoldOutException:
        # Event is live/future but the resale market is empty right now.
        # Save the event anyway so we can monitor it and alert when listings first appear.
        is_sold_out = True
        scraped_results, available_quantities = [], []
        scraped_date = None
    except Exception as e:
        import traceback
        logger.error(f"Add event failed: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Scrape failed: {type(e).__name__}: {e}"}
        )

    if not scraped_results and not is_sold_out:
        return JSONResponse(status_code=400, content={"error": "No listings found at that URL. The page may have failed to load — please try again."})

    name, slug_date = _parse_url_slug(url)
    # Prefer the date scraped from the page (more reliable than URL slug parsing).
    event_date = scraped_date or slug_date
    now = datetime.now(timezone.utc)

    event = Event(
        name=name,
        venue=None,
        event_date=event_date,
        ticketmaster_url=url,
        available_quantities=",".join(str(q) for q in available_quantities),
        quantity=available_quantities[0] if available_quantities else 1,
        added_at=now,
        is_active=True,
    )
    db.add(event)
    await db.flush()

    if scraped_results:
        listing_map: dict[tuple[str, int | None], int] = {}
        for r in scraped_results:
            db.add(Listing(
                event_id=event.id,
                name=r.name.strip(),
                quantity=r.quantity,
                is_available=True,
                last_seen_at=now,
            ))

        await db.flush()

        saved_listings_result = await db.execute(
            select(Listing).where(Listing.event_id == event.id)
        )
        for l in saved_listings_result.scalars().all():
            listing_map[(l.name.strip().lower(), l.quantity)] = l.id

        for r in scraped_results:
            lid = listing_map.get((r.name.strip().lower(), r.quantity))
            if lid and r.min_price > 0:
                db.add(PriceSnapshot(
                    listing_id=lid,
                    price=r.min_price,
                    scraped_at=now,
                ))

    await db.commit()

    # Populate venue sections from manifest (best-effort — does not block event creation)
    tm_event_id = url.rstrip("/").split("/")[-1]
    try:
        sections = await fetch_venue_sections(tm_event_id)
        if sections:
            for s in sections:
                db.add(VenueSection(
                    event_id=event.id,
                    name=s["name"],
                    is_ga=s["is_ga"],
                    num_seats=s["num_seats"],
                ))
            await db.commit()
            logger.info("Populated %d venue sections for event %d", len(sections), event.id)
        else:
            logger.warning("No venue sections returned from manifest for event %d (TM id: %s)", event.id, tm_event_id)
    except Exception:
        logger.warning("Failed to populate venue sections for event %d", event.id, exc_info=True)

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


@router.post("/events/{event_id}/acknowledge-date-change")
async def acknowledge_date_change(event_id: int, db: AsyncSession = Depends(get_db)):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.date_changed_at = None
    event.previous_event_date = None
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/events/{event_id}/dismiss-alert")
async def dismiss_alert(event_id: int, db: AsyncSession = Depends(get_db)):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    if not event_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Event not found")

    listing_ids_result = await db.execute(
        select(Listing.id)
        .join(UserWatch, UserWatch.listing_id == Listing.id)
        .where(Listing.event_id == event_id)
        .where(UserWatch.is_active == True)
    )
    listing_ids = [row[0] for row in listing_ids_result.all()]

    if listing_ids:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        await db.execute(
            delete(AlertLog)
            .where(AlertLog.listing_id.in_(listing_ids))
            .where(AlertLog.alerted_at >= cutoff)
        )
        await db.commit()

    return JSONResponse({"ok": True})


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

    remove_availability_job(event_id)
    await db.execute(delete(AlertHistoryLog).where(AlertHistoryLog.event_id == event_id))

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
        scraped_results, _, scraped_date = await loop.run_in_executor(
            None, _scrape_sync, event.ticketmaster_url
        )
    except EventEndedException:
        logger.info("Event %d detected as ended during manual scrape — marking ended.", event_id)
        event.is_ended = True
        event.is_active = False
        await db.commit()
        await _cancel_all_jobs_for_event(event_id)
        return JSONResponse({"ended": True})
    except SoldOutException:
        logger.info("Event %d is still sold out — no resale listings found.", event_id)
        return JSONResponse({"listings_found": 0, "listings": []})
    except Exception as e:
        logger.error("Manual scrape failed for event %d: %s", event_id, e)
        return JSONResponse(status_code=500, content={"error": f"Scrape failed: {e}"})

    from app.scheduler.engine import _check_and_update_date
    await _check_and_update_date(event, scraped_date, datetime.now(timezone.utc))

    await _reconcile_listings(db, event_id, scraped_results)

    listings_result = await db.execute(
        select(Listing).where(Listing.event_id == event_id)
    )
    all_listings = listings_result.scalars().all()
    listing_map: dict[tuple[str, int | None], int] = {
        (l.name.strip().lower(), l.quantity): l.id for l in all_listings
    }

    for r in scraped_results:
        lid = listing_map.get((r.name.strip().lower(), r.quantity))
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
    quantity: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    listing_result = await db.execute(select(Listing).where(Listing.id == listing_id))
    if not listing_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Listing not found")

    existing_result = await db.execute(
        select(UserWatch)
        .where(UserWatch.listing_id == listing_id)
        .where(UserWatch.quantity == quantity)
        .limit(1)
    )
    watch = existing_result.scalar_one_or_none()
    created = watch is None

    if watch:
        watch.target_price = target_price
        watch.refresh_interval_minutes = refresh_interval_minutes
        watch.alert_cooldown_minutes = alert_cooldown_minutes
        watch.quantity = quantity
        watch.is_active = True
    else:
        watch = UserWatch(
            listing_id=listing_id,
            target_price=target_price,
            refresh_interval_minutes=refresh_interval_minutes,
            alert_cooldown_minutes=alert_cooldown_minutes,
            quantity=quantity,
            is_active=True,
        )
        db.add(watch)
        await db.flush()

    schedule_watch_job(watch)
    await db.commit()
    return JSONResponse({"watch_id": watch.id, "created": created})


@router.delete("/watches/{watch_id}")
async def delete_watch_by_id(watch_id: int, db: AsyncSession = Depends(get_db)):
    watch_result = await db.execute(
        select(UserWatch).where(UserWatch.id == watch_id).limit(1)
    )
    watch = watch_result.scalar_one_or_none()
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    remove_watch_job(watch.id)
    await db.delete(watch)
    await db.commit()
    return JSONResponse({"deleted": True})


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
    )
    snapshots = result.scalars().all()
    return JSONResponse({
        "data": [
            {"timestamp": s.scraped_at.isoformat(), "price": s.price}
            for s in snapshots
        ]
    })


@router.post("/events/{event_id}/availability-watches")
async def create_availability_watch(
    event_id: int,
    section_name: str = Form(...),
    quantity: int = Form(...),
    target_price: float | None = Form(None),
    alert_cooldown_minutes: int = Form(60),
    db: AsyncSession = Depends(get_db),
):
    event_result = await db.execute(
        select(Event).where(Event.id == event_id)
    )
    if not event_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Event not found")

    existing_result = await db.execute(
        select(AvailabilityWatch)
        .where(AvailabilityWatch.event_id == event_id)
        .where(AvailabilityWatch.section_name == section_name.strip())
        .where(AvailabilityWatch.quantity == quantity)
    )
    watch = existing_result.scalar_one_or_none()

    if watch:
        watch.target_price = target_price
        watch.alert_cooldown_minutes = alert_cooldown_minutes
        watch.is_active = True
    else:
        watch = AvailabilityWatch(
            event_id=event_id,
            section_name=section_name.strip(),
            quantity=quantity,
            target_price=target_price,
            alert_cooldown_minutes=alert_cooldown_minutes,
            is_active=True,
        )
        db.add(watch)

    await db.flush()
    await db.commit()
    schedule_availability_job(event_id)
    return JSONResponse({"created": watch.id is None, "watch_id": watch.id})


@router.delete("/events/{event_id}/availability-watches/{watch_id}")
async def delete_availability_watch(
    event_id: int,
    watch_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AvailabilityWatch)
        .where(AvailabilityWatch.id == watch_id)
        .where(AvailabilityWatch.event_id == event_id)
    )
    watch = result.scalar_one_or_none()
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    await db.delete(watch)
    await db.commit()

    remaining = await db.execute(
        select(AvailabilityWatch)
        .where(AvailabilityWatch.event_id == event_id)
        .where(AvailabilityWatch.is_active == True)
        .limit(1)
    )
    if not remaining.scalar_one_or_none():
        remove_availability_job(event_id)

    return JSONResponse({"deleted": True})


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
# Event notes
# ---------------------------------------------------------------------------


@router.post("/events/{event_id}/notes")
async def update_event_notes(
    event_id: int,
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.notes = notes.strip() or None
    await db.commit()
    return JSONResponse({"notes": event.notes})


# ---------------------------------------------------------------------------
# Alert History
# ---------------------------------------------------------------------------


@router.get("/alerts/history", response_class=HTMLResponse)
async def alert_history(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AlertHistoryLog).order_by(desc(AlertHistoryLog.alerted_at))
    )
    logs = result.scalars().all()
    return templates.TemplateResponse(request, "alert_history.html", {"logs": logs})


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

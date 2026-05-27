import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from bs4 import BeautifulSoup, Tag
import httpx

from app.scraper.browser import managed_browser_context

logger = logging.getLogger(__name__)


class EventEndedException(Exception):
    """Raised when Ticketmaster indicates the event has passed."""
    pass

PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")

# Child selectors used to pull a listing name from a container element.
_NAME_CHILD_SELECTORS = [
    "[class*='name']",
    "[class*='section']",
    "[class*='zone']",
    "[class*='title']",
    "[class*='label']",
    "[class*='seat']",
    "strong",
    "h4",
    "h3",
    "h2",
]


@dataclass
class ListingResult:
    name: str
    min_price: float
    quantity: int | None = None


_executor = ThreadPoolExecutor(max_workers=1)


def scrape_event_sync(url: str) -> tuple[list[ListingResult], list[int]]:
    """
    Run scrape_event in a dedicated thread with its own event loop.
    Returns (listings, available_quantities). Always unfiltered — one result per section+quantity.
    """
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(scrape_event(url))
        finally:
            loop.close()

    future = _executor.submit(_run)
    return future.result(timeout=300)


# ---------------------------------------------------------------------------
# Manifest fetcher
# ---------------------------------------------------------------------------


async def fetch_venue_sections(event_id: str) -> list[dict]:
    """
    Fetches all venue sections from the Ticketmaster manifest API.
    Returns list of dicts with keys: name, is_ga, num_seats.
    Returns empty list on any failure.
    """
    url = f"https://pubapi.ticketmaster.ca/sdk/static/manifest/v1/{event_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning("Manifest API returned %d for event %s", resp.status_code, event_id)
            return []
        data = resp.json()
        sections = data.get("manifestSections", [])
        return [
            {
                "name": s["name"],
                "is_ga": bool(s.get("ga", False)),
                "num_seats": s.get("numSeats"),
            }
            for s in sections
            if s.get("name")
        ]
    except Exception:
        logger.warning("fetch_venue_sections failed for event %s", event_id, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def scrape_event(url: str) -> tuple[list[ListingResult], list[int]]:
    """Returns (listings, available_quantities). Always unfiltered — one result per section+quantity."""
    logger.info("Scrape started — %s", url)
    try:
        async with managed_browser_context() as ctx:
            results, available_quantities = await _scrape_page(ctx, url)
        logger.info(
            "Scrape finished — %d listing(s), available qty=%s for %s",
            len(results), available_quantities, url,
        )
        return results, available_quantities
    except EventEndedException:
        raise  # already logged in _check_event_ended; let callers handle it
    except Exception:
        logger.exception("Scrape failed for %s", url)
        raise


# ---------------------------------------------------------------------------
# Internal scraping steps
# ---------------------------------------------------------------------------


async def _scrape_page(context, url: str) -> tuple[list[ListingResult], list[int]]:
    page = await context.new_page()
    pending_responses = []

    def handle_response(response):
        resp_url = response.url
        if (
            response.status == 200
            and "ticketmaster" in resp_url
            and any(k in resp_url for k in ["quickpicks", "offers", "facets", "ismds"])
        ):
            pending_responses.append(response)
            logger.debug(f"Queued pricing response: {resp_url[:100]}")

    page.on("response", handle_response)

    try:
        await page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        logger.debug("Navigating to %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        await _accept_cookies(page)

        # Exit immediately if the event has already taken place — before the
        # 20 s offers-API wait so we never waste time on a dead page.
        await _check_event_ended(page, url)

        # Poll until the offers API response arrives, then close immediately.
        max_wait_ms = 20_000
        poll_interval_ms = 500
        elapsed = 0
        while elapsed < max_wait_ms:
            has_offers = any(
                "offers" in r.url and "ismds" in r.url
                for r in pending_responses
            )
            if has_offers:
                await page.wait_for_timeout(1_000)
                break
            await page.wait_for_timeout(poll_interval_ms)
            elapsed += poll_interval_ms
        else:
            logger.warning("Offers API response never arrived within 20s — attempting extraction anyway")

        # Read queued response bodies sequentially now that we have the data.
        captured_data = []
        for resp in pending_responses:
            try:
                body = await resp.body()
                data = json.loads(body.decode("utf-8"))
                captured_data.append(data)
            except Exception as e:
                logger.debug(f"Could not read body for {resp.url[:80]}: {e}")

        logger.debug(f"Successfully parsed {len(captured_data)} response bodies out of {len(pending_responses)} queued")

        available_quantities = extract_available_quantities(captured_data)

        # Approach A — parse captured API responses
        results = _extract_from_api_responses(captured_data)
        if results:
            logger.debug("API approach yielded %d result(s)", len(results))
            return results, available_quantities

        # Approach B — DOM extraction fallback
        logger.debug("API approach empty, falling back to DOM extraction")
        results = await _extract_listings(page)
        return results, available_quantities
    finally:
        await page.close()


async def _check_event_ended(page, url: str) -> None:
    """Raise EventEndedException if Ticketmaster signals the event has passed."""
    # Fast path: check for the specific DOM element TM renders on ended events.
    ended_el = await page.query_selector('[data-bdd="canceled-event-header-title"]')
    if ended_el is not None:
        logger.info("Ended event detected (DOM element) for URL: %s", url)
        raise EventEndedException(f"Event has ended: {url}")
    # Fallback: check for the canonical ended-event phrase in the raw HTML.
    html = await page.content()
    if "ticket sales have stopped" in html:
        logger.info("Ended event detected (phrase match) for URL: %s", url)
        raise EventEndedException(f"Event has ended: {url}")


async def _accept_cookies(page) -> None:
    for selector in (
        "button[id*='accept']",
        "button[id*='cookie']",
        "button[aria-label*='Accept']",
    ):
        try:
            await page.click(selector, timeout=2_000)
            logger.debug("Cookie banner dismissed")
            return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# API response parsing (Approach A)
# ---------------------------------------------------------------------------


def extract_available_quantities(responses: list[dict]) -> list[int]:
    """Return sorted unique sellableQuantities found across all offers in all responses."""
    seen: set[int] = set()
    for resp_data in responses:
        try:
            offers_list = resp_data.get("_embedded", {}).get("offer", [])
            for item in offers_list:
                if not isinstance(item, dict):
                    continue
                for q in item.get("sellableQuantities", []):
                    try:
                        seen.add(int(q))
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            logger.debug("extract_available_quantities error: %s", e)
    result = sorted(seen)
    logger.info("Available quantities from API: %s", result)
    return result


def _extract_from_api_responses(responses: list[dict]) -> list[ListingResult]:
    raw: list[ListingResult] = []

    for resp_data in responses:
        try:
            # Path A — _embedded.offer (singular)
            offers_list = resp_data.get("_embedded", {}).get("offer", [])
            if isinstance(offers_list, list) and offers_list:
                path_a: list[ListingResult] = []
                for item in offers_list:
                    if not isinstance(item, dict):
                        continue
                    if item.get("online") is False:
                        continue
                    if item.get("protected") is True:
                        continue
                    section = item.get("section")
                    if not section or not str(section).strip():
                        continue
                    price = item.get("listPrice") or item.get("totalPrice")
                    try:
                        price = float(price)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    sellable = item.get("sellableQuantities") or []
                    if not sellable:
                        sellable = [None]
                    for qty in sellable:
                        path_a.append(ListingResult(
                            name=str(section).strip(),
                            min_price=price,
                            quantity=int(qty) if qty is not None else None,
                        ))
                logger.debug("Path A (_embedded.offer): %d result(s)", len(path_a))
                raw.extend(path_a)

            # Path B — top-level "sections" list (no per-quantity data available)
            sections_list = resp_data.get("sections", [])
            if isinstance(sections_list, list) and sections_list:
                path_b: list[ListingResult] = []
                for item in sections_list:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name") or item.get("sectionName")
                    if not name or not str(name).strip():
                        continue
                    price = item.get("minPrice") or item.get("price") or item.get("currentPrice")
                    try:
                        price = float(price)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    path_b.append(ListingResult(name=str(name).strip(), min_price=price))
                logger.debug("Path B (sections): %d result(s)", len(path_b))
                raw.extend(path_b)

        except Exception as e:
            logger.debug("Error processing API response: %s", e)

    logger.info("API extraction: %d raw result(s) found", len(raw))

    # Deduplicate by (section name, quantity), keeping lowest price, sorted ascending.
    best: dict[tuple[str, int | None], ListingResult] = {}
    for r in raw:
        key = (r.name.lower(), r.quantity)
        if key not in best or r.min_price < best[key].min_price:
            best[key] = r

    result = sorted(best.values(), key=lambda r: r.min_price)
    logger.info("API extraction: %d unique section+quantity(s) after dedup", len(result))
    return result


# ---------------------------------------------------------------------------
# HTML extraction — three strategies with deduplication (Approach B)
# ---------------------------------------------------------------------------


async def _extract_listings(page) -> list[ListingResult]:
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    results = _strategy_a(soup)
    if results:
        logger.debug("Extraction strategy A yielded %d result(s)", len(results))
        return _deduplicate(results)

    results = _strategy_b(soup)
    if results:
        logger.debug("Extraction strategy B yielded %d result(s)", len(results))
        return _deduplicate(results)

    results = _strategy_c(soup)
    if results:
        logger.debug("Extraction strategy C yielded %d result(s)", len(results))
        return _deduplicate(results)

    logger.warning("All extraction strategies returned 0 listings")
    return []


def _strategy_a(soup: BeautifulSoup) -> list[ListingResult]:
    """Look for structured listing containers via data-testid or ticket/Ticket class names."""
    candidates: list[Tag] = []
    seen_ids: set[int] = set()

    for selector in (
        '[data-testid*="listing"]',
        '[class*="ticket"]',
        '[class*="Ticket"]',
    ):
        try:
            for el in soup.select(selector):
                if id(el) not in seen_ids:
                    seen_ids.add(id(el))
                    candidates.append(el)
        except Exception:
            pass

    results = []
    for el in candidates:
        text = el.get_text(separator=" ", strip=True)
        prices = _all_prices(text)
        if not prices:
            continue
        name = _extract_name_from_element(el)
        if not name:
            continue
        results.append(ListingResult(name=name, min_price=min(prices)))

    return results


def _strategy_b(soup: BeautifulSoup) -> list[ListingResult]:
    """Look for <tr> or <li> elements containing both a label and a price."""
    results = []
    for el in soup.find_all(["tr", "li"]):
        text = el.get_text(separator=" ", strip=True)
        prices = _all_prices(text)
        if not prices or len(text) < 4:
            continue
        name = _name_from_text(text)
        if not name:
            continue
        results.append(ListingResult(name=name, min_price=min(prices)))
    return results


def _strategy_c(soup: BeautifulSoup) -> list[ListingResult]:
    """Fallback: find all text nodes containing a price and group nearby text as name."""
    results = []
    for node in soup.find_all(string=PRICE_RE):
        parent = node.parent
        if parent is None:
            continue
        text = parent.get_text(separator=" ", strip=True)
        prices = _all_prices(text)
        if not prices:
            continue
        name = _name_from_text(text)
        if not name:
            name = "Unknown"
        results.append(ListingResult(name=name, min_price=min(prices)))
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_prices(text: str) -> list[float]:
    return [float(m.lstrip("$").replace(",", "")) for m in PRICE_RE.findall(text)]


def _extract_name_from_element(el: Tag) -> str:
    """Try child selectors first, then fall back to first non-price words."""
    for sel in _NAME_CHILD_SELECTORS:
        try:
            child = el.select_one(sel)
            if child:
                candidate = child.get_text(strip=True)
                if candidate and not PRICE_RE.match(candidate):
                    return candidate
        except Exception:
            pass
    return _name_from_text(el.get_text(separator=" ", strip=True))


def _name_from_text(text: str) -> str:
    """Strip prices and numerics, return first 6 meaningful words."""
    words = [
        w for w in text.split()
        if not PRICE_RE.match(w) and not w.replace(",", "").replace(".", "").isdigit()
    ]
    return " ".join(words[:6]).strip()


def _deduplicate(results: list[ListingResult]) -> list[ListingResult]:
    """Keep the lowest-priced entry per name (case-insensitive)."""
    best: dict[str, ListingResult] = {}
    for r in results:
        key = r.name.strip().lower()
        if not key:
            continue
        if key not in best or r.min_price < best[key].min_price:
            best[key] = r
    return list(best.values())



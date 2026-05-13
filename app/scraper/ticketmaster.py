import json
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from app.scraper.browser import managed_browser_context

logger = logging.getLogger(__name__)

PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")

# Selectors tried in order when waiting for ticket data to load.
_TICKET_WAIT_SELECTORS = [
    "[data-testid='listings-container']",
    "[class*='offer-list']",
    "[class*='OfferList']",
    "[class*='ticket-list']",
    "[class*='resale']",
    "[class*='Resale']",
    "li[class*='offer']",
]

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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def scrape_event(url: str) -> tuple[list[ListingResult], int]:
    """Returns (listings, api_response_count)."""
    logger.info("Scrape started — %s", url)
    try:
        async with managed_browser_context() as ctx:
            results, api_count = await _scrape_page(ctx, url)
        logger.info(
            "Scrape finished — %d listing(s), %d API response(s) for %s",
            len(results), api_count, url,
        )
        return results, api_count
    except Exception:
        logger.exception("Scrape failed for %s", url)
        raise


# ---------------------------------------------------------------------------
# Internal scraping steps
# ---------------------------------------------------------------------------


async def _scrape_page(context, url: str) -> tuple[list[ListingResult], int]:
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
        await page.wait_for_load_state("load", timeout=30_000)

        await _accept_cookies(page)
        await _wait_for_ticket_data(page)

        # Read queued response bodies sequentially now that the page has settled.
        captured_data = []
        for resp in pending_responses:
            try:
                body = await resp.body()
                data = json.loads(body.decode("utf-8"))
                captured_data.append(data)
            except Exception as e:
                logger.debug(f"Could not read body for {resp.url[:80]}: {e}")

        logger.debug(f"Successfully parsed {len(captured_data)} response bodies out of {len(pending_responses)} queued")

        # Approach A — parse captured API responses
        results = _extract_from_api_responses(captured_data)
        if results:
            logger.debug("API approach yielded %d result(s)", len(results))
            return results, len(captured_data)

        # Approach B — DOM extraction fallback
        logger.debug("API approach empty, falling back to DOM extraction")
        results = await _extract_listings(page)
        return results, len(captured_data)
    finally:
        await page.close()


async def _accept_cookies(page) -> None:
    for selector in (
        "button[id*='accept']",
        "button[id*='cookie']",
        "button[aria-label*='Accept']",
    ):
        try:
            await page.click(selector, timeout=3_000)
            logger.debug("Cookie banner dismissed")
            return
        except Exception:
            pass


async def _wait_for_ticket_data(page) -> None:
    await page.wait_for_timeout(8_000)
    for selector in _TICKET_WAIT_SELECTORS:
        try:
            await page.wait_for_selector(selector, timeout=20_000)
            logger.debug("Ticket data confirmed via '%s'", selector)
            return
        except Exception:
            pass
    logger.warning("No known ticket-data selector appeared — attempting extraction anyway")


# ---------------------------------------------------------------------------
# API response parsing (Approach A)
# ---------------------------------------------------------------------------


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
                    path_a.append(ListingResult(name=str(section).strip(), min_price=price))
                logger.debug("Path A (_embedded.offer): %d result(s)", len(path_a))
                raw.extend(path_a)

            # Path B — top-level "sections" list
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

    # Deduplicate by section name, keeping lowest price, sorted ascending.
    best: dict[str, ListingResult] = {}
    for r in raw:
        key = r.name.lower()
        if key not in best or r.min_price < best[key].min_price:
            best[key] = r

    result = sorted(best.values(), key=lambda r: r.min_price)
    logger.info("API extraction: %d unique section(s) after dedup", len(result))
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

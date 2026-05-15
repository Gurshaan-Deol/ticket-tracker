import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from app.scraper.browser import managed_browser_context

logger = logging.getLogger(__name__)

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


@dataclass
class RawOfferResult:
    section: str
    list_price: float
    sellable_quantities: str  # comma-separated ints, or "any" meaning no restriction
    inventory_type: str = "resale"


_executor = ThreadPoolExecutor(max_workers=1)


def scrape_event_sync(url: str, quantity: int | None = None) -> tuple[list[ListingResult], list[int], list[RawOfferResult]]:
    """
    Run scrape_event in a dedicated thread with its own event loop.
    Returns (listings, available_quantities, raw_offers).
    quantity=None means no filtering — return all listings and discover available quantities.
    """
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(scrape_event(url, quantity))
        finally:
            loop.close()

    future = _executor.submit(_run)
    return future.result(timeout=300)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def scrape_event(url: str, quantity: int | None = None) -> tuple[list[ListingResult], list[int], list[RawOfferResult]]:
    """Returns (listings, available_quantities, raw_offers). quantity=None → no filter."""
    qty_label = str(quantity) if quantity is not None else "all"
    logger.info("Scrape started — %s (qty=%s)", url, qty_label)
    try:
        async with managed_browser_context() as ctx:
            results, available_quantities, raw_offers = await _scrape_page(ctx, url, quantity)
        logger.info(
            "Scrape finished — %d listing(s), available qty=%s for %s",
            len(results), available_quantities, url,
        )
        return results, available_quantities, raw_offers
    except Exception:
        logger.exception("Scrape failed for %s", url)
        raise


# ---------------------------------------------------------------------------
# Internal scraping steps
# ---------------------------------------------------------------------------


async def _scrape_page(context, url: str, quantity: int | None = None) -> tuple[list[ListingResult], list[int], list[RawOfferResult]]:
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

        # Poll until the offers API response arrives, then wait for pagination to complete.
        max_wait_ms = 30_000
        poll_interval_ms = 500
        elapsed = 0
        while elapsed < max_wait_ms:
            has_offers = any(
                "offers" in r.url and "ismds" in r.url
                for r in pending_responses
            )
            if has_offers:
                # Wait longer to capture all paginated offer responses
                # Ticketmaster fires multiple offers requests in sequence
                await page.wait_for_timeout(3_000)
                break
            await page.wait_for_timeout(poll_interval_ms)
            elapsed += poll_interval_ms
        else:
            logger.warning("Offers API response never arrived within 30s — attempting extraction anyway")

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

        offer_responses = [
            (d, sum(1 for _ in d.get("_embedded", {}).get("offer", [])))
            for d in captured_data
            if d.get("_embedded", {}).get("offer")
        ]
        logger.info(f"Offer responses captured: {len(offer_responses)}")
        for i, (d, count) in enumerate(offer_responses):
            logger.info(f"  Offer batch {i+1}: {count} offers")

        available_quantities = extract_available_quantities(captured_data)
        raw_offers = extract_raw_offers(captured_data)

        # Approach A — parse captured API responses
        results = _extract_from_api_responses(captured_data, quantity)
        if results:
            logger.debug("API approach yielded %d result(s)", len(results))
            return results, available_quantities, raw_offers

        # Approach B — DOM extraction fallback
        logger.debug("API approach empty, falling back to DOM extraction")
        results = await _extract_listings(page)
        return results, available_quantities, raw_offers
    finally:
        await page.close()


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


def extract_raw_offers(responses: list[dict]) -> list[RawOfferResult]:
    """Return all individual offers from API responses without deduplication."""
    results: list[RawOfferResult] = []
    for resp_data in responses:
        try:
            offers_list = resp_data.get("_embedded", {}).get("offer", [])
            if not isinstance(offers_list, list):
                continue
            for offer in offers_list:
                if not isinstance(offer, dict):
                    continue
                if offer.get("online") is False:
                    continue
                if offer.get("protected") is True:
                    continue
                section = offer.get("section")
                if not section or not str(section).strip():
                    continue
                list_price = offer.get("listPrice") or offer.get("totalPrice")
                if list_price is None:
                    continue
                try:
                    list_price = float(list_price)
                except (TypeError, ValueError):
                    continue
                if list_price <= 0:
                    continue
                raw_sq = offer.get("sellableQuantities", [])
                if raw_sq:
                    sellable_quantities = [int(q) for q in raw_sq if str(q).isdigit()]
                    sq_str = ",".join(str(q) for q in sellable_quantities)
                else:
                    sq_str = "any"
                inventory_type = offer.get("inventoryType", "resale")
                results.append(RawOfferResult(
                    section=str(section).strip(),
                    list_price=list_price,
                    sellable_quantities=sq_str,
                    inventory_type=inventory_type,
                ))
        except Exception as e:
            logger.debug("extract_raw_offers error: %s", e)
    logger.debug("extract_raw_offers: %d raw offer(s)", len(results))
    return results


def _extract_from_api_responses(responses: list[dict], quantity: int | None = None) -> list[ListingResult]:
    raw: list[ListingResult] = []

    for resp_data in responses:
        try:
            # Path A — _embedded.offer (singular)
            offers_list = resp_data.get("_embedded", {}).get("offer", [])
            if isinstance(offers_list, list) and offers_list and quantity is not None:
                all_sellable = set()
                for item in offers_list:
                    sq = item.get("sellableQuantities", [])
                    if sq:
                        all_sellable.update(sq)
                logger.debug(f"Quantity filter={quantity}, all sellableQuantities found: {sorted(all_sellable)}")
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
                    if quantity is not None:
                        sellable = item.get("sellableQuantities")
                        if sellable and quantity not in sellable:
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

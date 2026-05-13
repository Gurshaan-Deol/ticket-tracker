import asyncio
import logging
import random
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

SCRAPE_LOCK = asyncio.Lock()

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--start-maximized",
]

_STEALTH = Stealth()


def get_random_ua() -> str:
    return random.choice(USER_AGENTS)


@asynccontextmanager
async def managed_browser_context():
    await SCRAPE_LOCK.acquire()
    pw = None
    browser = None
    context = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=False, args=_LAUNCH_ARGS)
        ua = get_random_ua()
        context = await browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/Toronto",
        )
        await _STEALTH.apply_stealth_async(context)
        logger.debug("Browser context ready — UA: %.60s", ua)
        yield context
    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass
        SCRAPE_LOCK.release()

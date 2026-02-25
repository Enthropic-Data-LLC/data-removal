"""FastPeopleSearch broker plugin.

Opt-out: Online form at https://www.fastpeoplesearch.com/removal
Difficulty: Easy
Expected time: 72h

FastPeopleSearch uses Cloudflare WAF plus reCAPTCHA on the opt-out
form.  This plugin tries stealth headless first and falls back to a
visible browser when captcha is detected.  Email verification is
required after submitting the removal form.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from dataremoval.brokers import (
    BrokerInfo,
    BrokerPlugin,
    Difficulty,
    OptOutMethod,
    register_broker,
)
from dataremoval.brokers._utils import (
    DEFAULT_USER_AGENT,
    HAS_PLAYWRIGHT,
    check_url_status,
    compute_confidence,
    deduplicate,
    launch_browser,
    stealth_playwright,
    wait_for_captcha,
)
from dataremoval.core.models import Listing, Profile

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.fastpeoplesearch.com"
REMOVAL_URL = f"{BASE_URL}/removal"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 30_000
SEARCH_DELAY_SECONDS = 3

# CSS selectors for result cards (may need tuning against live DOM)
_RESULT_CARD_SEL = "div.card"
_RESULT_LINK_SEL = "a[href*='/name/']"
_RESULT_NAME_SEL = ".card-title, h4"
_RESULT_LOCATION_SEL = ".card-location, .card-address"
_RESULT_AGE_SEL = ".card-age"

# Profile detail URLs: /name/<First>-<Last>_<City>-<State>
_PROFILE_URL_RE = re.compile(r"/name/[A-Za-z]+-[A-Za-z]+", re.IGNORECASE)

# Captcha detection (Cloudflare + reCAPTCHA)
_CAPTCHA_INDICATOR_SEL = (
    "iframe[src*='challenge'], div.cf-turnstile, iframe[src*='recaptcha'], div.g-recaptcha"
)


# ---------------------------------------------------------------------------
# Pure helper functions (testable without browser)
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_url(first: str, last: str, city: str = "", state: str = "") -> str:
    """Build a FastPeopleSearch search URL.

    Uses underscore between name and location segments.

    >>> _build_search_url("Jane", "Smith", "Springfield", "IL")
    'https://www.fastpeoplesearch.com/name/Jane-Smith_Springfield-IL'
    """
    name_slug = _normalize_name(f"{first} {last}")
    location = f"{city} {state}".strip()
    if location:
        return f"{BASE_URL}/name/{name_slug}_{_normalize_name(location)}"
    return f"{BASE_URL}/name/{name_slug}"


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate FastPeopleSearch URLs from all profile search variants."""
    urls: list[str] = []
    for variant in profile.search_variants():
        first = variant.get("first_name", "")
        last = variant.get("last_name", "")
        if not first or not last:
            continue
        city = variant.get("city", "")
        state = variant.get("state", "")
        urls.append(_build_search_url(first, last, city, state))
    return urls


def _is_profile_url(url: str) -> bool:
    """Return True if *url* looks like a FastPeopleSearch person detail page."""
    path = urlparse(url).path
    return bool(_PROFILE_URL_RE.search(path))


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


async def _handle_captcha(page: Page, description: str = "page") -> bool:
    return await wait_for_captcha(page, description=description, selector=_CAPTCHA_INDICATOR_SEL)


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------


async def _extract_card(card, profile: Profile) -> Listing | None:
    """Extract listing data from a single result card element."""
    # Detail link
    link_el = await card.query_selector(_RESULT_LINK_SEL)
    href = ""
    if link_el:
        href = (await link_el.get_attribute("href")) or ""
    if href and not href.startswith("http"):
        href = f"{BASE_URL}{href}"
    if not _is_profile_url(href):
        return None

    # Name
    name_el = await card.query_selector(_RESULT_NAME_SEL)
    found_name = (await name_el.inner_text()).strip() if name_el else ""

    # Location
    loc_el = await card.query_selector(_RESULT_LOCATION_SEL)
    found_location = (await loc_el.inner_text()).strip() if loc_el else ""

    # Age
    age_el = await card.query_selector(_RESULT_AGE_SEL)
    found_age = (await age_el.inner_text()).strip() if age_el else ""

    confidence = compute_confidence(profile, found_name, found_location, found_age)

    return Listing(
        broker_id="fastpeoplesearch",
        profile_id=profile.id,
        url=href,
        found_name=found_name,
        found_location=found_location,
        found_age=found_age,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class FastPeopleSearchPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="fastpeoplesearch",
            name="FastPeopleSearch",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=REMOVAL_URL,
            difficulty=Difficulty.EASY,
            expected_days=3,
            recheck_days=90,
            notes=(
                "Uses Cloudflare + reCAPTCHA. Stealth headless with visible "
                "fallback. Email verification required."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search FastPeopleSearch for listings matching *profile*.

        Tries stealth headless first.  If Cloudflare captcha is detected,
        relaunches a visible browser for manual captcha solving.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping FastPeopleSearch search")
            return []

        urls = _build_search_urls(profile)
        if not urls:
            return []

        listings: list[Listing] = []

        async with stealth_playwright() as pw:
            # Probe headless first
            headless = True
            browser = await launch_browser(pw, headless=True)
            try:
                page: Page = await browser.new_page(user_agent=USER_AGENT)
                with contextlib.suppress(Exception):
                    await page.goto(urls[0], timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                captcha_el = await page.query_selector(_CAPTCHA_INDICATOR_SEL)
                if captcha_el:
                    headless = False
                    log.info("Cloudflare captcha detected — switching to visible browser")
            finally:
                await browser.close()

            # Launch the real browser (visible if captcha was detected)
            browser = await launch_browser(pw, headless=headless)
            try:
                page = await browser.new_page(user_agent=USER_AGENT)

                for i, url in enumerate(urls):
                    if i > 0:
                        await asyncio.sleep(SEARCH_DELAY_SECONDS)

                    try:
                        await page.goto(
                            url,
                            timeout=PAGE_TIMEOUT_MS,
                            wait_until="domcontentloaded",
                        )
                    except Exception:
                        log.debug("Navigation timeout for %s", url)
                        continue

                    # Handle captcha — user solves in visible window
                    if not await _handle_captcha(page, description=url):
                        log.warning("Skipping %s due to unsolved captcha", url)
                        continue

                    # Wait for result cards
                    try:
                        await page.wait_for_selector(_RESULT_CARD_SEL, timeout=PAGE_TIMEOUT_MS)
                    except Exception:
                        log.debug("No result cards found for %s", url)
                        continue

                    cards = await page.query_selector_all(_RESULT_CARD_SEL)
                    for card in cards:
                        try:
                            listing = await _extract_card(card, profile)
                            if listing:
                                listings.append(listing)
                        except Exception:
                            log.debug("Failed to extract card on %s", url)
                            continue
            finally:
                await browser.close()

        return deduplicate(listings)

    async def submit_opt_out(self, listing: Listing) -> bool:
        """Submit a removal request via FastPeopleSearch's removal page.

        Tries stealth headless; falls back to visible browser if captcha
        is detected.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False

        try:
            async with stealth_playwright() as pw:
                # Probe headless first
                headless = True
                browser = await launch_browser(pw, headless=True)
                try:
                    probe = await browser.new_page(user_agent=USER_AGENT)
                    with contextlib.suppress(Exception):
                        await probe.goto(
                            REMOVAL_URL,
                            timeout=PAGE_TIMEOUT_MS,
                            wait_until="domcontentloaded",
                        )
                    captcha_el = await probe.query_selector(_CAPTCHA_INDICATOR_SEL)
                    if captcha_el:
                        headless = False
                        log.info("Cloudflare captcha detected — switching to visible browser")
                finally:
                    await browser.close()

                browser = await launch_browser(pw, headless=headless)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    # Navigate to the removal page
                    await page.goto(
                        REMOVAL_URL,
                        timeout=PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )

                    if not await _handle_captcha(page, description="removal page"):
                        log.error("Could not pass captcha on removal page")
                        return False

                    # Look for a URL/name input and fill it
                    url_input = page.locator(
                        'input[name="url"], '
                        'input[placeholder*="fastpeoplesearch.com"], '
                        'input[type="url"], '
                        'input[type="text"]'
                    ).first
                    await url_input.fill(listing.url)

                    # Click the remove / submit button
                    remove_btn = page.locator(
                        'button:has-text("Remove"), '
                        'button[type="submit"], '
                        'a:has-text("Remove My Record"), '
                        'input[type="submit"]'
                    ).first
                    await remove_btn.click(timeout=PAGE_TIMEOUT_MS)

                    # Handle any confirmation captcha
                    if not await _handle_captcha(page, description="removal confirmation"):
                        log.error("Could not pass captcha on removal confirmation")
                        return False

                    # Wait for confirmation
                    await page.wait_for_selector(
                        "text=/removed|success|request.*received|check your email|"
                        "email.*verification|record.*removed/i",
                        timeout=PAGE_TIMEOUT_MS,
                    )

                    log.info("Successfully submitted removal for %s", listing.url)
                    return True
                finally:
                    await browser.close()

        except Exception:
            log.exception("Opt-out submission failed for %s", listing.url)
            return False

    async def check_status(self, listing: Listing) -> str:
        """Check whether a listing URL still resolves (httpx, no browser)."""
        return await check_url_status(listing.url)


register_broker(FastPeopleSearchPlugin())

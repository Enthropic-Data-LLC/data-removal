"""Intelius broker plugin.

Opt-out: Online form + email verification, or mail-in
Difficulty: Medium
Expected time: 72h

Intelius is part of PeopleConnect (also owns TruthFinder and Instant
Checkmate).  Opting out from Intelius also removes data from those
sister sites.  The opt-out flow goes through the "Do Not Sell My
Personal Information" link and requires email verification.

Mail-in: PO Box 24025, Seattle, WA 98124
"""

from __future__ import annotations

import asyncio
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

BASE_URL = "https://www.intelius.com"
OPT_OUT_URL = f"{BASE_URL}/opt-out"
MAIL_ADDRESS = "PO Box 24025, Seattle, WA 98124"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 30_000
SEARCH_DELAY_SECONDS = 3

# CSS selectors for search result cards
_RESULT_CARD_SEL = ".record-card, .search-result"
_RESULT_LINK_SEL = "a[href*='/people-search/']"
_RESULT_NAME_SEL = ".record-name, .result-name, h4"
_RESULT_LOCATION_SEL = ".record-location, .result-location"
_RESULT_AGE_SEL = ".record-age, .result-age"

# Profile URLs: /people-search/First-Last/State/
_PROFILE_URL_RE = re.compile(r"/people-search/[A-Za-z-]+/[A-Za-z-]+/")

# Captcha detection
_CAPTCHA_INDICATOR_SEL = (
    "iframe[src*='recaptcha'], div.g-recaptcha, iframe[src*='challenge'], div.cf-turnstile"
)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for Intelius URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_url(first: str, last: str, state: str = "") -> str:
    """Build an Intelius search URL.

    >>> _build_search_url("Jane", "Smith", "IL")
    'https://www.intelius.com/people-search/Jane-Smith/IL/'
    """
    name_slug = _normalize_name(f"{first} {last}")
    url = f"{BASE_URL}/people-search/{name_slug}"
    if state:
        url += f"/{_normalize_name(state)}"
    return url + "/"


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate Intelius search URLs from all profile search variants."""
    urls: list[str] = []
    for variant in profile.search_variants():
        first = variant.get("first_name", "")
        last = variant.get("last_name", "")
        if not first or not last:
            continue
        state = variant.get("state", "")
        urls.append(_build_search_url(first, last, state))
    return urls


def _is_profile_url(url: str) -> bool:
    """Return True if *url* looks like an Intelius person profile page."""
    path = urlparse(url).path
    return bool(_PROFILE_URL_RE.search(path))


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------


async def _extract_card(card, profile: Profile) -> Listing | None:
    """Extract listing data from a single search result card."""
    link_el = await card.query_selector(_RESULT_LINK_SEL)
    href = ""
    if link_el:
        href = (await link_el.get_attribute("href")) or ""
    if href and not href.startswith("http"):
        href = f"{BASE_URL}{href}"
    if not _is_profile_url(href):
        return None

    name_el = await card.query_selector(_RESULT_NAME_SEL)
    found_name = (await name_el.inner_text()).strip() if name_el else ""

    loc_el = await card.query_selector(_RESULT_LOCATION_SEL)
    found_location = (await loc_el.inner_text()).strip() if loc_el else ""

    age_el = await card.query_selector(_RESULT_AGE_SEL)
    found_age = (await age_el.inner_text()).strip() if age_el else ""

    confidence = compute_confidence(profile, found_name, found_location, found_age)

    return Listing(
        broker_id="intelius",
        profile_id=profile.id,
        url=href,
        found_name=found_name,
        found_location=found_location,
        found_age=found_age,
        confidence=confidence,
    )


async def _handle_captcha(page: Page, description: str = "page") -> bool:
    return await wait_for_captcha(page, description=description, selector=_CAPTCHA_INDICATOR_SEL)


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class InteliusPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="intelius",
            name="Intelius",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=OPT_OUT_URL,
            difficulty=Difficulty.MEDIUM,
            expected_days=3,
            recheck_days=90,
            mail_address=MAIL_ADDRESS,
            notes=(
                "Part of PeopleConnect — opting out also covers TruthFinder and "
                "Instant Checkmate. Email verification required. Also supports "
                "mail-in opt-out."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search Intelius for listings matching *profile*."""
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping Intelius search")
            return []

        urls = _build_search_urls(profile)
        if not urls:
            return []

        listings: list[Listing] = []

        async with stealth_playwright() as pw:
            browser = await launch_browser(pw, headless=True)
            try:
                page: Page = await browser.new_page(user_agent=USER_AGENT)

                for i, url in enumerate(urls):
                    if i > 0:
                        await asyncio.sleep(SEARCH_DELAY_SECONDS)

                    try:
                        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                    except Exception:
                        log.debug("Navigation timeout for %s", url)
                        continue

                    if not await _handle_captcha(page, description=url):
                        log.warning("Skipping %s due to unsolved captcha", url)
                        continue

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
        """Submit a removal request via Intelius opt-out page.

        Navigates to the "Do Not Sell" opt-out flow: search for the
        record, select it, and submit.  Email verification must be
        completed separately by the user.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False

        try:
            async with stealth_playwright() as pw:
                browser = await launch_browser(pw, headless=False)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    # Navigate to opt-out page
                    await page.goto(
                        OPT_OUT_URL, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded"
                    )

                    if not await _handle_captcha(page, description="opt-out page"):
                        log.error("Could not pass captcha on opt-out page")
                        return False

                    # Fill search fields
                    name_input = page.locator(
                        'input[name="name"], input[placeholder*="name" i], input[name="firstName"]'
                    ).first
                    await name_input.fill(listing.found_name)

                    # Click search
                    search_btn = page.locator(
                        'button:has-text("Search"), button:has-text("Find"), button[type="submit"]'
                    ).first
                    await search_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    if not await _handle_captcha(page, description="search results"):
                        return False

                    # Select the record
                    record_btn = page.locator(
                        'button:has-text("This is me"), button:has-text("Select"), '
                        'a:has-text("This is me")'
                    ).first
                    await record_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    # Submit removal
                    submit_btn = page.locator(
                        'button:has-text("Submit"), button:has-text("Opt Out"), '
                        'button:has-text("Remove"), button[type="submit"]'
                    ).first
                    await submit_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    if not await _handle_captcha(page, description="confirmation"):
                        return False

                    # Wait for confirmation
                    await page.wait_for_selector(
                        "text=/check your email|request.*received|opt.?out.*submitted|"
                        "removal.*confirmed|success/i",
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


register_broker(InteliusPlugin())

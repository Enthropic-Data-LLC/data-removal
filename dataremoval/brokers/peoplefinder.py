"""PeopleFinder broker plugin.

Opt-out: Multi-step online form at https://www.peoplefinder.com/opt-out
Difficulty: Medium
Expected time: 3-9 days

PeopleFinder has a multi-step opt-out form: search, select record,
provide reason, enter email, verify.  Each step may have reCAPTCHA.
This plugin uses playwright-stealth for headless search and a visible
browser for the opt-out flow.
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

BASE_URL = "https://www.peoplefinder.com"
OPT_OUT_URL = f"{BASE_URL}/opt-out"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 30_000
SEARCH_DELAY_SECONDS = 3

# CSS selectors for search result cards
_RESULT_CARD_SEL = ".people-result, .result-card"
_RESULT_LINK_SEL = "a[href*='/people/']"
_RESULT_NAME_SEL = ".name, .result-name, h4"
_RESULT_LOCATION_SEL = ".location, .result-location"
_RESULT_AGE_SEL = ".age, .result-age"

# Profile URLs: /people/First-Last/State/id
_PROFILE_URL_RE = re.compile(r"/people/[A-Za-z-]+/[A-Za-z-]+/\w+")

# Captcha detection
_CAPTCHA_INDICATOR_SEL = (
    "iframe[src*='recaptcha'], div.g-recaptcha, iframe[src*='challenge'], div.cf-turnstile"
)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for PeopleFinder URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_url(first: str, last: str, city: str = "", state: str = "") -> str:
    """Build a PeopleFinder search URL.

    >>> _build_search_url("Jane", "Smith", "Springfield", "IL")
    'https://www.peoplefinder.com/people/Jane-Smith/Springfield-IL'
    """
    name_slug = _normalize_name(f"{first} {last}")
    url = f"{BASE_URL}/people/{name_slug}"
    location = f"{city} {state}".strip()
    if location:
        url += f"/{_normalize_name(location)}"
    return url


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate PeopleFinder search URLs from all profile search variants."""
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
    """Return True if *url* looks like a PeopleFinder person profile page."""
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
        broker_id="peoplefinder",
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


class PeopleFinderPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="peoplefinder",
            name="PeopleFinder",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=OPT_OUT_URL,
            difficulty=Difficulty.MEDIUM,
            expected_days=9,
            recheck_days=90,
            notes=(
                "Multi-step opt-out form: search, select, reason, email, verify. "
                "Each step may have reCAPTCHA. 3-9 days processing."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search PeopleFinder for listings matching *profile*."""
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping PeopleFinder search")
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
        """Submit a removal request via PeopleFinder's multi-step opt-out form.

        Opens a visible browser for the multi-step process: search for
        the record, select it, provide a reason, enter email, and submit.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False

        try:
            async with stealth_playwright() as pw:
                browser = await launch_browser(pw, headless=False)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    # Step 1: Navigate to opt-out page
                    await page.goto(
                        OPT_OUT_URL, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded"
                    )

                    if not await _handle_captcha(page, description="opt-out page"):
                        log.error("Could not pass captcha on opt-out page")
                        return False

                    # Step 2: Fill search fields
                    name_input = page.locator(
                        'input[name="name"], input[placeholder*="name" i], input[name="firstName"]'
                    ).first
                    await name_input.fill(listing.found_name)

                    # Click search
                    search_btn = page.locator(
                        'button:has-text("Search"), button[type="submit"]'
                    ).first
                    await search_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    if not await _handle_captcha(page, description="search results"):
                        return False

                    # Step 3: Select the record
                    record_btn = page.locator(
                        'button:has-text("This is me"), button:has-text("Select"), '
                        'a:has-text("This is me")'
                    ).first
                    await record_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    # Step 4: Select reason (if dropdown)
                    try:
                        reason_select = page.locator("select, [role='listbox']").first
                        await reason_select.select_option(index=1, timeout=5000)
                    except Exception:
                        log.debug("No reason dropdown found, continuing")

                    # Step 5: Submit removal
                    submit_btn = page.locator(
                        'button:has-text("Submit"), button:has-text("Remove"), '
                        'button[type="submit"]'
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


register_broker(PeopleFinderPlugin())

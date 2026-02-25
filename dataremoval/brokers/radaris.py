"""Radaris broker plugin.

Opt-out: Online form at https://radaris.com/control/privacy
Difficulty: Easy
Expected time: 24h

Radaris uses Cloudflare Turnstile bot protection.  This plugin uses
playwright-stealth for headless search.  The profile page has a
'Remove My Information' option that initiates the opt-out flow.
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

BASE_URL = "https://radaris.com"
OPT_OUT_URL = f"{BASE_URL}/control/privacy"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 30_000
SEARCH_DELAY_SECONDS = 3

# CSS selectors for search result cards
_RESULT_CARD_SEL = ".person-card"
_RESULT_LINK_SEL = "a.person-link"
_RESULT_NAME_SEL = ".person-name"
_RESULT_LOCATION_SEL = ".person-location"
_RESULT_AGE_SEL = ".person-age"

# Profile URLs: /p/First-Last/ patterns
_PROFILE_URL_RE = re.compile(r"/p/[A-Za-z]+-[A-Za-z-]+/")

# Captcha detection (Cloudflare Turnstile)
_CAPTCHA_INDICATOR_SEL = (
    "div.cf-turnstile, iframe[src*='challenge'], "
    "iframe[src*='turnstile'], iframe[src*='recaptcha'], div.g-recaptcha"
)


# ---------------------------------------------------------------------------
# Pure helper functions (testable without browser)
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for Radaris URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_url(first: str, last: str) -> str:
    """Build a Radaris search URL.

    >>> _build_search_url("Jane", "Smith")
    'https://radaris.com/p/Jane-Smith/'
    """
    name_slug = _normalize_name(f"{first} {last}")
    return f"{BASE_URL}/p/{name_slug}/"


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate Radaris search URLs from all profile search variants."""
    urls: list[str] = []
    seen: set[str] = set()
    for variant in profile.search_variants():
        first = variant.get("first_name", "")
        last = variant.get("last_name", "")
        if not first or not last:
            continue
        url = _build_search_url(first, last)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _is_profile_url(url: str) -> bool:
    """Return True if *url* looks like a Radaris person profile page."""
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
        broker_id="radaris",
        profile_id=profile.id,
        url=href,
        found_name=found_name,
        found_location=found_location,
        found_age=found_age,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Captcha helper
# ---------------------------------------------------------------------------


async def _handle_captcha(page: Page, description: str = "page") -> bool:
    return await wait_for_captcha(page, description=description, selector=_CAPTCHA_INDICATOR_SEL)


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class RadarisPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="radaris",
            name="Radaris",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=OPT_OUT_URL,
            difficulty=Difficulty.EASY,
            expected_days=1,
            recheck_days=90,
            notes=(
                "Cloudflare Turnstile protected. Profile page has 'Remove My Information' option."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search Radaris for listings matching *profile*.

        Uses playwright-stealth for headless operation.  If a Cloudflare
        Turnstile captcha appears, waits for it to be resolved.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed -- skipping Radaris search")
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
                        await page.goto(
                            url,
                            timeout=PAGE_TIMEOUT_MS,
                            wait_until="domcontentloaded",
                        )
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
        """Submit a removal request via Radaris privacy control page.

        Navigates to the profile page, clicks 'Remove My Information',
        and follows the opt-out flow through the privacy control page.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed -- cannot submit opt-out")
            return False

        try:
            async with stealth_playwright() as pw:
                browser = await launch_browser(pw, headless=False)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    # Navigate to the listing profile page first
                    await page.goto(
                        listing.url,
                        timeout=PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )

                    if not await _handle_captcha(page, description="profile page"):
                        log.error("Could not pass captcha on profile page")
                        return False

                    # Click "Remove My Information" on the profile page
                    remove_link = page.locator(
                        'a:has-text("Remove My Information"), '
                        'a:has-text("Remove Information"), '
                        'button:has-text("Remove"), '
                        "a.remove-link"
                    ).first
                    await remove_link.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    if not await _handle_captcha(page, description="removal page"):
                        return False

                    # Confirm removal on the privacy control page
                    confirm_btn = page.locator(
                        'button:has-text("Confirm"), '
                        'button:has-text("Submit"), '
                        'button:has-text("Remove"), '
                        'button[type="submit"]'
                    ).first
                    await confirm_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    # Wait for confirmation
                    await page.wait_for_selector(
                        "text=/removed|success|request.*received|opt.?out.*submitted/i",
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


register_broker(RadarisPlugin())

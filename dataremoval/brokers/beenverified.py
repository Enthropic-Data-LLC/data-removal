"""BeenVerified broker plugin.

Opt-out: Online form at https://www.beenverified.com/svc/optout/search/optouts
Difficulty: Easy
Expected time: 48h

BeenVerified has a dedicated opt-out search page separate from its main
search.  The opt-out flow requires reCAPTCHA completion and email
verification.  This plugin uses playwright-stealth for headless search
and opens a visible browser for opt-out submission.
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

BASE_URL = "https://www.beenverified.com"
OPT_OUT_URL = f"{BASE_URL}/svc/optout/search/optouts"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 30_000
SEARCH_DELAY_SECONDS = 3

# CSS selectors for search result cards
_RESULT_CARD_SEL = ".people-card"
_RESULT_LINK_SEL = "a.people-link"
_RESULT_NAME_SEL = ".people-name"
_RESULT_LOCATION_SEL = ".people-location"
_RESULT_AGE_SEL = ".people-age"

# Profile URLs: /people/First-Last/State/P<id>
_PROFILE_URL_RE = re.compile(r"/people/[A-Za-z-]+/[A-Za-z]+/P[a-zA-Z0-9]+")

# Captcha detection (reCAPTCHA)
_CAPTCHA_INDICATOR_SEL = (
    "iframe[src*='recaptcha'], div.g-recaptcha, iframe[src*='challenge'], div.cf-turnstile"
)


# ---------------------------------------------------------------------------
# Pure helper functions (testable without browser)
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for BeenVerified URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_url(first: str, last: str, state: str = "") -> str:
    """Build a BeenVerified search URL.

    >>> _build_search_url("Jane", "Smith", "Illinois")
    'https://www.beenverified.com/people/Jane-Smith/Illinois/'
    """
    name_slug = _normalize_name(f"{first} {last}")
    url = f"{BASE_URL}/people/{name_slug}"
    if state:
        url += f"/{_normalize_name(state)}"
    return url + "/"


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate BeenVerified search URLs from all profile search variants."""
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
    """Return True if *url* looks like a BeenVerified person profile page."""
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
        broker_id="beenverified",
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


class BeenVerifiedPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="beenverified",
            name="BeenVerified",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=OPT_OUT_URL,
            difficulty=Difficulty.EASY,
            expected_days=2,
            recheck_days=90,
            notes=(
                "Dedicated opt-out search page. Email verification required. reCAPTCHA protected."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search BeenVerified for listings matching *profile*.

        Uses playwright-stealth for headless operation.  If a reCAPTCHA
        appears, waits for it to be resolved.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed -- skipping BeenVerified search")
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
        """Submit an opt-out request via BeenVerified's dedicated opt-out page.

        Navigates to the opt-out search, fills in the person details,
        handles reCAPTCHA, and submits the removal request.  Email
        verification must be completed separately by the user.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed -- cannot submit opt-out")
            return False

        try:
            async with stealth_playwright() as pw:
                browser = await launch_browser(pw, headless=False)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    await page.goto(
                        OPT_OUT_URL,
                        timeout=PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )

                    if not await _handle_captcha(page, description="opt-out page"):
                        log.error("Could not pass captcha on opt-out page")
                        return False

                    # Fill the listing URL or name fields
                    url_input = page.locator(
                        'input[name="url"], '
                        'input[placeholder*="beenverified"], '
                        'input[type="url"], '
                        "input.optout-input"
                    ).first
                    await url_input.fill(listing.url)

                    # Submit the form
                    submit_btn = page.locator(
                        'button:has-text("Submit"), '
                        'button:has-text("Search"), '
                        'button:has-text("Send"), '
                        'button[type="submit"]'
                    ).first
                    await submit_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    if not await _handle_captcha(page, description="opt-out confirm"):
                        return False

                    # Wait for confirmation
                    await page.wait_for_selector(
                        "text=/check your email|confirmation|opt.?out.*submitted|request.*received/i",
                        timeout=PAGE_TIMEOUT_MS,
                    )

                    log.info("Successfully submitted opt-out for %s", listing.url)
                    return True
                finally:
                    await browser.close()

        except Exception:
            log.exception("Opt-out submission failed for %s", listing.url)
            return False

    async def check_status(self, listing: Listing) -> str:
        """Check whether a listing URL still resolves (httpx, no browser)."""
        return await check_url_status(listing.url)


register_broker(BeenVerifiedPlugin())

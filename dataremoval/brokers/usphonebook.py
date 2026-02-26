"""USPhonebook broker plugin.

Opt-out: Online form at https://www.usphonebook.com/opt-out/submit
Difficulty: Easy
Expected time: 72h

USPhonebook is Cloudflare-protected.  This plugin uses playwright-stealth
to bypass bot detection headlessly.  The opt-out flow requires email
verification -- the plugin automates form filling up to that step.
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

BASE_URL = "https://www.usphonebook.com"
OPT_OUT_URL = f"{BASE_URL}/removal"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 30_000
SEARCH_DELAY_SECONDS = 3

# CSS selectors for search result cards (verified against live DOM 2026-02)
_RESULT_CARD_SEL = "div.success-wrapper-padding"
_RESULT_LINK_SEL = "a.ls_contacts-btn"
_RESULT_NAME_SEL = "h3.ls_number-text span[itemprop='name']"
_RESULT_LOCATION_SEL = "span.ls_success-black-text[itemprop='address']"

# Profile URLs: /<first>-<last>/<encoded-id>  e.g. /david-brown/UEjM4kDMwQDO...
_PROFILE_URL_RE = re.compile(r"/[a-z]+-[a-z]+/U[A-Za-z0-9]+R$", re.IGNORECASE)

# Captcha detection (Cloudflare challenge)
_CAPTCHA_INDICATOR_SEL = (
    "iframe[src*='challenge'], div.cf-turnstile, iframe[src*='recaptcha'], div.g-recaptcha"
)


# ---------------------------------------------------------------------------
# Pure helper functions (testable without browser)
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for USPhonebook URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


# State abbreviation to full name for URL construction
_STATE_NAMES = {
    "AL": "alabama",
    "AK": "alaska",
    "AZ": "arizona",
    "AR": "arkansas",
    "CA": "california",
    "CO": "colorado",
    "CT": "connecticut",
    "DE": "delaware",
    "FL": "florida",
    "GA": "georgia",
    "HI": "hawaii",
    "ID": "idaho",
    "IL": "illinois",
    "IN": "indiana",
    "IA": "iowa",
    "KS": "kansas",
    "KY": "kentucky",
    "LA": "louisiana",
    "ME": "maine",
    "MD": "maryland",
    "MA": "massachusetts",
    "MI": "michigan",
    "MN": "minnesota",
    "MS": "mississippi",
    "MO": "missouri",
    "MT": "montana",
    "NE": "nebraska",
    "NV": "nevada",
    "NH": "new hampshire",
    "NJ": "new jersey",
    "NM": "new mexico",
    "NY": "new york",
    "NC": "north carolina",
    "ND": "north dakota",
    "OH": "ohio",
    "OK": "oklahoma",
    "OR": "oregon",
    "PA": "pennsylvania",
    "RI": "rhode island",
    "SC": "south carolina",
    "SD": "south dakota",
    "TN": "tennessee",
    "TX": "texas",
    "UT": "utah",
    "VT": "vermont",
    "VA": "virginia",
    "WA": "washington",
    "WV": "west virginia",
    "WI": "wisconsin",
    "WY": "wyoming",
    "DC": "district of columbia",
}


def _build_search_url(first: str, last: str, city: str = "", state: str = "") -> str:
    """Build a USPhonebook people-search URL.

    >>> _build_search_url("Jane", "Smith", "Springfield", "IL")
    'https://www.usphonebook.com/jane-smith/illinois/springfield'
    """
    name_slug = _normalize_name(f"{first} {last}").lower()
    url = f"{BASE_URL}/{name_slug}"
    state_full = _STATE_NAMES.get(state.upper(), state.lower()) if state else ""
    if state_full:
        url += f"/{state_full.replace(' ', '%20')}"
    if city:
        url += f"/{_normalize_name(city).lower()}"
    return url


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate USPhonebook search URLs from all profile search variants."""
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
    """Return True if *url* looks like a USPhonebook person detail page."""
    path = urlparse(url).path
    return bool(_PROFILE_URL_RE.search(path))


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------


async def _extract_card(card, profile: Profile) -> Listing | None:
    """Extract listing data from a single search result card."""
    # Detail link: "VIEW FULL ADDRESS & PHONE" button
    link_el = await card.query_selector(_RESULT_LINK_SEL)
    href = ""
    if link_el:
        href = (await link_el.get_attribute("href")) or ""
    if href and not href.startswith("http"):
        href = f"{BASE_URL}{href}"
    if not _is_profile_url(href):
        return None

    # Name: <span itemprop="name">
    name_el = await card.query_selector(_RESULT_NAME_SEL)
    found_name = (await name_el.inner_text()).strip() if name_el else ""

    # Location: <span itemprop="address">
    loc_el = await card.query_selector(_RESULT_LOCATION_SEL)
    found_location = (await loc_el.inner_text()).strip() if loc_el else ""

    # Age: inline text in h3.ls_number-text like "David Brown, Age 59"
    found_age = ""
    heading_el = await card.query_selector("h3.ls_number-text")
    if heading_el:
        heading_text = (await heading_el.inner_text()).strip()
        age_match = re.search(r"Age\s+(\d+)", heading_text)
        if age_match:
            found_age = age_match.group(1)

    # Relatives from links
    relative_els = await card.query_selector_all("a.ls_success-blue-link span")
    found_relatives = []
    for rel_el in relative_els:
        rel_text = (await rel_el.inner_text()).strip().rstrip(",")
        if rel_text:
            found_relatives.append(rel_text)

    confidence = compute_confidence(
        profile, found_name, found_location, found_age, found_relatives=found_relatives
    )

    return Listing(
        broker_id="usphonebook",
        profile_id=profile.id,
        url=href,
        found_name=found_name,
        found_location=found_location,
        found_age=found_age,
        confidence=confidence,
        raw_data={"relatives": found_relatives} if found_relatives else {},
    )


# ---------------------------------------------------------------------------
# Captcha helper
# ---------------------------------------------------------------------------


async def _handle_captcha(page: Page, description: str = "page") -> bool:
    return await wait_for_captcha(page, description=description, selector=_CAPTCHA_INDICATOR_SEL)


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class USPhonebookPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="usphonebook",
            name="USPhonebook",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=OPT_OUT_URL,
            difficulty=Difficulty.EASY,
            expected_days=3,
            recheck_days=90,
            notes="Cloudflare protected. Email verification required for opt-out.",
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search USPhonebook for listings matching *profile*.

        Uses playwright-stealth for headless operation.  If a Cloudflare
        captcha appears, waits for it to be resolved.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed -- skipping USPhonebook search")
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
        """Submit an opt-out request via USPhonebook's removal form.

        The form requires first name, last name, email, agreement checkbox,
        and reCAPTCHA. Opens a visible browser for captcha solving.
        Email verification must be completed separately by the user.
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

                    if not await _handle_captcha(page, description="removal page"):
                        log.error("Could not pass Cloudflare on removal page")
                        return False

                    # Fill the removal form fields
                    first = listing.found_name.split()[0] if listing.found_name else ""
                    last = listing.found_name.split()[-1] if listing.found_name else ""
                    email = listing.raw_data.get("opt_out_email", "")

                    await page.fill("#subject-firstname", first)
                    await page.fill("#subject-lastname", last)
                    if email:
                        await page.fill("#subject-email", email)

                    # Check the agreement checkbox
                    await page.click("#agreement")

                    # reCAPTCHA — user must solve in visible browser
                    log.warning(
                        "Please solve the reCAPTCHA and click 'Begin Removal Process' "
                        "in the browser window. Waiting up to 300 seconds..."
                    )

                    # Wait for confirmation (page redirect or success message)
                    await page.wait_for_selector(
                        "text=/check your email|confirmation|opt.?out.*submitted|"
                        "request.*received|verify your email/i",
                        timeout=300_000,
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


register_broker(USPhonebookPlugin())

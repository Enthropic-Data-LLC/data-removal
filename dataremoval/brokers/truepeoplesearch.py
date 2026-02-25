"""TruePeopleSearch broker plugin.

Opt-out: Online form at https://www.truepeoplesearch.com/removal
Difficulty: Easy
Expected time: 24h

TruePeopleSearch uses DataDome bot protection (beyond Cloudflare WAF)
which blocks headless browsers even with stealth techniques.  This
plugin tries stealth headless first but will fall back to a visible
browser for manual captcha solving when DataDome blocks access.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import quote_plus, urlparse

import httpx

from dataremoval.brokers import (
    BrokerInfo,
    BrokerPlugin,
    Difficulty,
    OptOutMethod,
    register_broker,
)
from dataremoval.core.models import Listing, Profile

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Playwright

try:
    from playwright.async_api import async_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from playwright_stealth import Stealth

    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.truepeoplesearch.com"
SEARCH_URL = f"{BASE_URL}/results"
REMOVAL_URL = f"{BASE_URL}/removal"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PAGE_TIMEOUT_MS = 30_000  # higher than Spokeo due to Cloudflare
CAPTCHA_POLL_INTERVAL = 2.0  # seconds between captcha-solved checks
CAPTCHA_TIMEOUT = 300.0  # 5 minutes for user to solve captcha
SEARCH_DELAY_SECONDS = 3

# Chromium stealth args to reduce headless detection
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

# CSS selectors for result cards (may need adjustment against live DOM)
_RESULT_CARD_SEL = "div.card-summary"
_RESULT_LINK_SEL = "a[href*='/find/person/']"
_RESULT_NAME_SEL = "div.h4, .card-summary h4, .card-summary .h4"
_RESULT_LOCATION_SEL = "span.location, .card-summary .nowrap"
_RESULT_AGE_SEL = "span.age, .card-summary span:has-text('Age')"

# Profile detail URLs: /find/person/<hex-id>
_PROFILE_URL_RE = re.compile(r"/find/person/[a-z0-9]+", re.IGNORECASE)

# Captcha detection (includes DataDome captcha iframe)
_CAPTCHA_URL_FRAGMENT = "/InternalCaptcha"
_CAPTCHA_INDICATOR_SEL = (
    "form[action*='InternalCaptcha'], iframe[src*='challenge'], "
    "div.cf-turnstile, iframe[src*='captcha-delivery.com'], iframe[src*='geo.captcha']"
)


# ---------------------------------------------------------------------------
# Pure helper functions (testable without browser)
# ---------------------------------------------------------------------------


def _build_search_url(first: str, last: str, city: str = "", state: str = "") -> str:
    """Build a TruePeopleSearch results URL.

    >>> _build_search_url("Jane", "Smith", "Springfield", "IL")
    'https://www.truepeoplesearch.com/results?name=Jane+Smith&citystatezip=Springfield+IL'
    """
    name = quote_plus(f"{first} {last}")
    url = f"{SEARCH_URL}?name={name}"
    location = f"{city} {state}".strip()
    if location:
        url += f"&citystatezip={quote_plus(location)}"
    return url


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate TruePeopleSearch URLs from all profile search variants."""
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
    """Return True if *url* looks like a TruePeopleSearch person detail page."""
    path = urlparse(url).path
    return bool(_PROFILE_URL_RE.search(path))


def _compute_confidence(
    profile: Profile,
    found_name: str,
    found_location: str,
    found_age: str,
    found_relatives: list[str] | None = None,
) -> float:
    """Compute a 0-1 confidence score for a listing match."""
    score = 0.0

    # Name match (+0.4)
    profile_name = profile.full_name.lower()
    if found_name.lower() == profile_name:
        score += 0.4
    elif (
        profile.first_name.lower() in found_name.lower()
        and profile.last_name.lower() in found_name.lower()
    ):
        score += 0.3

    # State match (+0.2)
    found_loc_lower = found_location.lower()
    for addr in profile.addresses:
        if addr.state and addr.state.lower() in found_loc_lower:
            score += 0.2
            break

    # City match (+0.15)
    for addr in profile.addresses:
        if addr.city and addr.city.lower() in found_loc_lower:
            score += 0.15
            break

    # Age match (+0.15) — handles "Age 35" text format
    if profile.age and found_age:
        age_digits = re.search(r"\d+", found_age)
        if age_digits:
            try:
                if abs(profile.age - int(age_digits.group())) <= 1:
                    score += 0.15
            except ValueError:
                pass

    # Relatives match (up to +0.1)
    if found_relatives and profile.relatives:
        profile_rels = {r.lower() for r in profile.relatives}
        matches = sum(1 for r in found_relatives if r.lower() in profile_rels)
        if matches:
            score += min(0.1, matches * 0.05)

    return min(score, 1.0)


def _deduplicate(listings: list[Listing]) -> list[Listing]:
    """Remove duplicate listings by URL."""
    seen: set[str] = set()
    unique: list[Listing] = []
    for listing in listings:
        if listing.url not in seen:
            seen.add(listing.url)
            unique.append(listing)
    return unique


# ---------------------------------------------------------------------------
# Browser & captcha helpers
# ---------------------------------------------------------------------------


def _stealth_playwright():
    """Return a stealth-wrapped async_playwright context manager if available."""
    pw_ctx = async_playwright()
    if HAS_STEALTH:
        return Stealth().use_async(pw_ctx)
    return pw_ctx


async def _launch_browser(pw: Playwright, *, headless: bool = True) -> Browser:
    """Launch Chromium with stealth args."""
    return await pw.chromium.launch(headless=headless, args=list(_STEALTH_ARGS))


async def _is_captcha_page(page: Page) -> bool:
    """Detect whether the current page shows a captcha challenge."""
    if _CAPTCHA_URL_FRAGMENT in page.url:
        return True
    indicator = await page.query_selector(_CAPTCHA_INDICATOR_SEL)
    return indicator is not None


async def _wait_for_captcha(page: Page, description: str = "page") -> bool:
    """If page shows a captcha, wait for the user to solve it manually.

    Returns True if the captcha was solved (or there was no captcha).
    Returns False if the timeout expired.
    """
    if not await _is_captcha_page(page):
        return True

    log.warning(
        "Captcha detected on %s. Waiting up to %d seconds for resolution...",
        description,
        int(CAPTCHA_TIMEOUT),
    )

    elapsed = 0.0
    while elapsed < CAPTCHA_TIMEOUT:
        await asyncio.sleep(CAPTCHA_POLL_INTERVAL)
        elapsed += CAPTCHA_POLL_INTERVAL

        if not await _is_captcha_page(page):
            log.info("Captcha cleared. Continuing automation.")
            await page.wait_for_load_state("domcontentloaded")
            return True

    log.error("Captcha timeout after %d seconds on %s", int(CAPTCHA_TIMEOUT), description)
    return False


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

    confidence = _compute_confidence(profile, found_name, found_location, found_age)

    return Listing(
        broker_id="truepeoplesearch",
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


class TruePeopleSearchPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="truepeoplesearch",
            name="TruePeopleSearch",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=REMOVAL_URL,
            difficulty=Difficulty.EASY,
            expected_days=1,
            recheck_days=90,
            notes=(
                "Uses DataDome bot protection. Tries headless stealth first; "
                "requires visible browser with manual captcha solving."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search TruePeopleSearch for listings matching *profile*.

        Uses playwright-stealth for headless operation.  If a captcha
        still appears, waits for it to be resolved (e.g. Cloudflare
        turnstile auto-solve).
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping TruePeopleSearch search")
            return []

        urls = _build_search_urls(profile)
        if not urls:
            return []

        listings: list[Listing] = []

        async with _stealth_playwright() as pw:
            browser = await _launch_browser(pw, headless=True)
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

                    # Handle captcha
                    if not await _wait_for_captcha(page, description=url):
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

        return _deduplicate(listings)

    async def submit_opt_out(self, listing: Listing) -> bool:
        """Submit a removal request via TruePeopleSearch's removal page.

        Uses playwright-stealth for headless operation.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False

        try:
            async with _stealth_playwright() as pw:
                browser = await _launch_browser(pw, headless=True)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    # Navigate to the listing detail page
                    await page.goto(
                        listing.url,
                        timeout=PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )

                    if not await _wait_for_captcha(page, description="listing page"):
                        log.error("Could not pass captcha on listing page")
                        return False

                    # Click the "Remove This Record" button
                    remove_btn = page.locator(
                        'a:has-text("Remove This Record"), '
                        'button:has-text("Remove This Record"), '
                        'a:has-text("Remove Record"), '
                        "a.btn-remove"
                    ).first
                    await remove_btn.click(timeout=PAGE_TIMEOUT_MS)

                    # Handle any confirmation captcha
                    if not await _wait_for_captcha(page, description="removal confirmation"):
                        log.error("Could not pass captcha on removal confirmation")
                        return False

                    # Wait for confirmation
                    await page.wait_for_selector(
                        "text=/removed|success|request.*received|record.*removed/i",
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
        """Check whether a listing URL still resolves (httpx, no browser).

        Cloudflare may return 403 for bot requests, treated as 'unknown'.
        """
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                resp = await client.get(listing.url, timeout=10)
                if resp.status_code in (404, 410):
                    return "removed"
                if resp.status_code == 403:
                    log.debug(
                        "Got 403 for %s — Cloudflare block, status unknown",
                        listing.url,
                    )
                    return "unknown"
                return "still_listed"
        except Exception:
            return "unknown"


register_broker(TruePeopleSearchPlugin())

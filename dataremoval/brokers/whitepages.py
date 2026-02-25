"""Whitepages broker plugin.

Opt-out: Online form + phone call verification
Difficulty: Easy
Expected time: 48h

Whitepages uses JavaScript-rendered pages and may have Cloudflare
protection.  This plugin tries headless stealth mode first, then
falls back to a visible browser for manual interaction.  The opt-out
flow requires phone call verification — the plugin automates form
filling and pauses for the user to answer the call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

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

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.whitepages.com"
SUPPRESSION_URL = f"{BASE_URL}/suppression-requests"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PAGE_TIMEOUT_MS = 30_000
CAPTCHA_POLL_INTERVAL = 2.0
CAPTCHA_TIMEOUT = 300.0  # 5 minutes for user to solve captcha
PHONE_VERIFY_TIMEOUT = 300.0  # 5 minutes for phone verification
SEARCH_DELAY_SECONDS = 3

# Chromium stealth args
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

# CSS selectors for search result cards (may need tuning against live DOM)
_RESULT_CARD_SEL = "div.serp-card, div[data-testid='person-card']"
_RESULT_LINK_SEL = "a[href*='/name/']"
_RESULT_NAME_SEL = "a.h4, .serp-card h4, .serp-card .h4"
_RESULT_LOCATION_SEL = "span.location, .serp-card .address"
_RESULT_AGE_SEL = "span.age, .serp-card .age"

# Profile URLs: /name/First-Last/City-State
_PROFILE_URL_RE = re.compile(r"/name/[A-Za-z]+-[A-Za-z]+/[A-Za-z]+-[A-Za-z]+")

# Captcha detection
_CAPTCHA_INDICATOR_SEL = (
    "iframe[src*='challenge'], div.cf-turnstile, iframe[src*='recaptcha'], div.g-recaptcha"
)


# ---------------------------------------------------------------------------
# Pure helper functions (testable without browser)
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for Whitepages URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_url(first: str, last: str, city: str = "", state: str = "") -> str:
    """Build a Whitepages search URL.

    >>> _build_search_url("Jane", "Smith", "Springfield", "IL")
    'https://www.whitepages.com/name/Jane-Smith/Springfield-IL'
    """
    name_slug = _normalize_name(f"{first} {last}")
    url = f"{BASE_URL}/name/{name_slug}"
    location = f"{city} {state}".strip()
    if location:
        url += f"/{_normalize_name(location)}"
    return url


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate Whitepages search URLs from all profile search variants."""
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
    """Return True if *url* looks like a Whitepages person profile page."""
    from urllib.parse import urlparse

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

    # Age match (+0.15)
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


async def _launch_browser(pw: Playwright, *, headless: bool = True) -> Browser:
    """Launch Chromium with stealth args when headless."""
    args = list(_STEALTH_ARGS) if headless else []
    return await pw.chromium.launch(headless=headless, args=args)


async def _is_captcha_page(page: Page) -> bool:
    """Detect whether the current page shows a captcha challenge."""
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
        "Captcha detected on %s. Please solve it in the browser window. "
        "Waiting up to %d seconds...",
        description,
        int(CAPTCHA_TIMEOUT),
    )

    elapsed = 0.0
    while elapsed < CAPTCHA_TIMEOUT:
        await asyncio.sleep(CAPTCHA_POLL_INTERVAL)
        elapsed += CAPTCHA_POLL_INTERVAL

        if not await _is_captcha_page(page):
            log.info("Captcha solved. Continuing automation.")
            await page.wait_for_load_state("domcontentloaded")
            return True

    log.error("Captcha timeout after %d seconds on %s", int(CAPTCHA_TIMEOUT), description)
    return False


async def _needs_visible_browser(pw: Playwright, test_url: str) -> bool:
    """Try stealth headless on *test_url*; return True if captcha appears."""
    browser = await _launch_browser(pw, headless=True)
    try:
        page = await browser.new_page(user_agent=USER_AGENT)
        try:
            await page.goto(test_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception:
            return True  # navigation failure → fall back to visible
        return await _is_captcha_page(page)
    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------


async def _extract_card(card, profile: Profile) -> Listing | None:
    """Extract listing data from a single search result card."""
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
        broker_id="whitepages",
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


class WhitepagesPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="whitepages",
            name="Whitepages",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=SUPPRESSION_URL,
            difficulty=Difficulty.EASY,
            expected_days=2,
            recheck_days=90,
            notes=(
                "Requires phone call verification for opt-out. "
                "Stealth headless first; opens visible browser if needed. "
                "Re-lists quarterly from public records."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search Whitepages for listings matching *profile*.

        Tries headless stealth first.  If captcha appears, relaunches
        a visible browser for the user to solve it.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping Whitepages search")
            return []

        urls = _build_search_urls(profile)
        if not urls:
            return []

        listings: list[Listing] = []

        async with async_playwright() as pw:
            # Probe first URL to decide headless vs visible
            use_visible = await _needs_visible_browser(pw, urls[0])
            if use_visible:
                log.info("Captcha detected — launching visible browser for manual solving")

            browser = await _launch_browser(pw, headless=not use_visible)
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
        """Submit a removal request via Whitepages suppression form.

        Automates form filling up to the phone verification step, then
        prompts the user to answer the automated call and enter the code.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False

        try:
            async with async_playwright() as pw:
                # Always use visible browser for opt-out (needs phone interaction)
                browser = await _launch_browser(pw, headless=False)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    # Step 1: Navigate to suppression page
                    await page.goto(
                        SUPPRESSION_URL,
                        timeout=PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )

                    if not await _wait_for_captcha(page, description="suppression page"):
                        log.error("Could not pass captcha on suppression page")
                        return False

                    # Step 2: Paste listing URL and click Next
                    url_input = page.locator(
                        'input[name="url"], '
                        'input[placeholder*="whitepages.com"], '
                        'input[type="url"], '
                        "input.suppression-input"
                    ).first
                    await url_input.fill(listing.url)

                    next_btn = page.locator(
                        'button:has-text("Next"), button:has-text("next"), input[type="submit"]'
                    ).first
                    await next_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    if not await _wait_for_captcha(page, description="profile confirm"):
                        return False

                    # Step 3: Click "Remove Me"
                    remove_btn = page.locator(
                        'button:has-text("Remove Me"), '
                        'button:has-text("Remove me"), '
                        'a:has-text("Remove Me")'
                    ).first
                    await remove_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded")

                    # Step 4: Select reason (if dropdown appears)
                    try:
                        reason_select = page.locator("select, [role='listbox']").first
                        await reason_select.select_option(index=1, timeout=5000)
                    except Exception:
                        log.debug("No reason dropdown found, continuing")

                    # Step 5: Phone verification — user must interact
                    log.warning(
                        "Phone verification required. Please complete the phone "
                        "call verification in the browser window. "
                        "Waiting up to %d seconds...",
                        int(PHONE_VERIFY_TIMEOUT),
                    )

                    # Wait for confirmation that verification is complete
                    try:
                        await page.wait_for_selector(
                            "text=/opt.?out.*complete|request.*received|"
                            "successfully.*removed|removal.*confirmed/i",
                            timeout=PHONE_VERIFY_TIMEOUT * 1000,
                        )
                    except Exception:
                        log.error(
                            "Phone verification not completed within timeout for %s",
                            listing.url,
                        )
                        return False

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
                        "Got 403 for %s — possible Cloudflare block, status unknown",
                        listing.url,
                    )
                    return "unknown"
                return "still_listed"
        except Exception:
            return "unknown"


register_broker(WhitepagesPlugin())

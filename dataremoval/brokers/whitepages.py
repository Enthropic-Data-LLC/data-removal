"""Whitepages broker plugin.

Opt-out: Online form + phone call verification
Difficulty: Easy
Expected time: 48h

Whitepages uses JavaScript-rendered pages and may have Cloudflare
protection.  This plugin uses playwright-stealth to bypass bot
detection headlessly.  The opt-out flow requires phone call
verification — the plugin automates form filling up to that step,
then opens a visible browser for the user to complete the call.
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

BASE_URL = "https://www.whitepages.com"
SUPPRESSION_URL = f"{BASE_URL}/suppression-requests"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 30_000
PHONE_VERIFY_TIMEOUT = 300.0  # 5 minutes for phone verification
SEARCH_DELAY_SECONDS = 3

# CSS selectors for search result cards (verified against live DOM 2025-02)
_RESULT_CARD_SEL = "li.serp-card"
_RESULT_LINK_SEL = 'a[data-qa-selector="organic-card-person-name"]'
_RESULT_NAME_SEL = 'a[data-qa-selector="organic-card-person-name"]'
_RESULT_LOCATION_SEL = 'dd[data-qa-selector="serp-city-label"]'
_RESULT_AGE_SEL = ".person-age"

# TOS acceptance
_TOS_CHECKBOX_SEL = "#tos-checkbox"
_TOS_CONTINUE_SEL = 'button:has-text("Continue")'

# Profile URLs: /name/First-Last/City-State/P<id>
_PROFILE_URL_RE = re.compile(r"/name/[A-Za-z-]+/[A-Za-z-]+/P[a-zA-Z0-9]+")

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
    path = urlparse(url).path
    return bool(_PROFILE_URL_RE.search(path))


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


async def _accept_tos(page: Page) -> bool:
    """Accept Whitepages TOS modal if present. Returns True on success."""
    try:
        checkbox = await page.query_selector(_TOS_CHECKBOX_SEL)
        if not checkbox:
            return True  # no TOS modal
        await checkbox.click()
        await asyncio.sleep(0.5)
        btn = await page.query_selector(_TOS_CONTINUE_SEL)
        if btn:
            await btn.click()
            await page.wait_for_load_state("domcontentloaded")
            log.debug("Accepted TOS modal")
        return True
    except Exception:
        log.debug("TOS acceptance failed or not needed")
        return True


async def _handle_captcha(page: Page, description: str = "page") -> bool:
    return await wait_for_captcha(page, description=description, selector=_CAPTCHA_INDICATOR_SEL)


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

    confidence = compute_confidence(profile, found_name, found_location, found_age)

    return Listing(
        broker_id="whitepages",
        profile_id=profile.id,
        url=href,
        found_name=found_name,
        found_location=found_location,
        found_age=found_age,
        confidence=confidence,
    )


async def _extract_profile_phones(page: Page, url: str) -> list[str]:
    """Visit a Whitepages profile page and extract visible phone numbers."""
    phones: list[str] = []
    try:
        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        # Landline numbers are visible (not behind paywall)
        phone_links = await page.query_selector_all('a[href^="/phone/"]')
        for link in phone_links:
            text = (await link.inner_text()).strip()
            # Match phone format like (509) 448-3843
            if re.search(r"\(\d{3}\)\s*\d{3}-\d{4}", text):
                phones.append(text)
    except Exception:
        log.debug("Could not extract phones from %s", url)
    return phones


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
            requires_interaction=True,
            notes=(
                "Requires phone call verification for opt-out. "
                "Runs headless with playwright-stealth for search. "
                "Opens visible browser for opt-out (phone verification). "
                "Re-lists quarterly from public records."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search Whitepages for listings matching *profile*.

        Uses playwright-stealth for headless operation.  If a captcha
        still appears, waits for it to be resolved.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping Whitepages search")
            return []

        urls = _build_search_urls(profile)
        if not urls:
            return []

        listings: list[Listing] = []

        async with stealth_playwright() as pw:
            browser = await launch_browser(pw, headless=True)
            try:
                page: Page = await browser.new_page(user_agent=USER_AGENT)
                tos_accepted = False

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
                    if not await _handle_captcha(page, description=url):
                        log.warning("Skipping %s due to unsolved captcha", url)
                        continue

                    # Accept TOS modal (once per session)
                    if not tos_accepted:
                        tos_accepted = await _accept_tos(page)

                    # Wait a moment for content to settle after TOS
                    await asyncio.sleep(2)

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

                # Visit profile pages of high-confidence matches to grab phones
                for listing in listings:
                    if listing.confidence >= 0.5 and not listing.raw_data.get("phones"):
                        phones = await _extract_profile_phones(page, listing.url)
                        if phones:
                            listing.raw_data["phones"] = phones
                            log.debug("Collected %d phone(s) from %s", len(phones), listing.url)
            finally:
                await browser.close()

        return deduplicate(listings)

    async def submit_opt_out(self, listing: Listing) -> bool:
        """Submit a removal request via Whitepages suppression form.

        The Whitepages opt-out is a multi-step process:
          1. Paste profile URL → Next
          2. Confirm identity → phone number verification
          3. Confirmation

        Uses type() instead of fill() to properly trigger Vue.js reactivity
        on the suppression form.  Auto-fills the phone number from the
        listing's raw_data (collected during scan) or profile phone numbers.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False

        # Get phone number for verification (prefer listing data, fall back to profile)
        phone = ""
        if listing.raw_data.get("phones"):
            phone = listing.raw_data["phones"][0]
        if not phone:
            try:
                from dataremoval.core.database import Database

                db = Database()
                profile = db.get_profile(listing.profile_id)
                if profile and profile.phone_numbers:
                    phone = profile.phone_numbers[0]
            except Exception:
                log.debug("Could not load profile phone numbers")

        try:
            async with stealth_playwright() as pw:
                # Visible browser needed for phone verification interaction
                browser = await launch_browser(pw, headless=False)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    # Step 1: Navigate to suppression page
                    await page.goto(
                        SUPPRESSION_URL,
                        timeout=PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )
                    await asyncio.sleep(2)

                    if not await _handle_captcha(page, description="suppression page"):
                        log.error("Could not pass captcha on suppression page")
                        return False

                    # Step 1: Type listing URL (type() triggers Vue v-model, fill() does not)
                    url_input = page.locator("#suppression-requests-person-url")
                    await url_input.click()
                    await url_input.press_sequentially(listing.url, delay=20)
                    await asyncio.sleep(1)

                    next_btn = page.locator('button:has-text("Next")').first
                    await next_btn.click(timeout=PAGE_TIMEOUT_MS)

                    # Wait for step to advance (step 2 should load)
                    await asyncio.sleep(3)
                    await page.wait_for_load_state("domcontentloaded")

                    # Check for "not able to locate" error
                    error_el = page.locator("text=/not able to locate/i")
                    if await error_el.count() > 0:
                        log.error("Whitepages could not locate listing for URL: %s", listing.url)
                        return False

                    if not await _handle_captcha(page, description="profile confirm"):
                        return False

                    # Step 2: Confirm identity — look for "Remove Me" or proceed
                    try:
                        remove_btn = page.locator('button:has-text("Remove Me")').first
                        await remove_btn.click(timeout=10_000)
                        await asyncio.sleep(2)
                    except Exception:
                        log.debug("No 'Remove Me' button found, continuing")

                    # Step 3: Select reason if present
                    try:
                        reason_select = page.locator("select").first
                        await reason_select.select_option(
                            label="I just want to keep my information private",
                            timeout=5_000,
                        )
                        await asyncio.sleep(1)
                        next_btn2 = page.locator('button:has-text("Next")').first
                        await next_btn2.click(timeout=10_000)
                        await asyncio.sleep(2)
                    except Exception:
                        log.debug("No reason step found, continuing")

                    # Step 4: Phone verification
                    # Wait for phone input to appear
                    try:
                        await page.wait_for_selector(
                            "#suppression-requests-phone-number",
                            timeout=10_000,
                        )
                    except Exception:
                        log.debug("Phone input not found, may already be past this step")

                    if phone:
                        try:
                            phone_input = page.locator(
                                "#suppression-requests-phone-number"
                            )
                            await phone_input.click()
                            await phone_input.press_sequentially(
                                re.sub(r"[^\d]", "", phone), delay=30
                            )
                            log.info("Auto-filled phone number for verification")

                            # Check the consent checkbox
                            checkbox = page.locator(
                                'input[type="checkbox"]'
                            ).first
                            await checkbox.click()
                            await asyncio.sleep(0.5)

                            # Click "Call now to verify"
                            call_btn = page.locator(
                                'button:has-text("Call now to verify")'
                            ).first
                            await call_btn.click(timeout=10_000)
                            log.info(
                                "Initiated verification call to %s",
                                phone,
                            )
                        except Exception:
                            log.debug("Could not auto-fill phone, user must complete manually")

                    log.warning(
                        "Phone verification required. Answer the call and enter "
                        "the verification code in the browser. Waiting up to %d seconds...",
                        int(PHONE_VERIFY_TIMEOUT),
                    )

                    # Wait for completion confirmation
                    try:
                        await page.wait_for_selector(
                            "text=/opt.?out.*complete|request.*received|"
                            "successfully.*removed|removal.*confirmed|"
                            "has been removed|step 5/i",
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
        """Check whether a listing URL still resolves (httpx, no browser)."""
        return await check_url_status(listing.url)


register_broker(WhitepagesPlugin())

"""BeenVerified broker plugin.

Opt-out: Online form at https://www.beenverified.com/svc/optout/search/optouts
Difficulty: Medium
Expected time: 24-48h

BeenVerified's opt-out requires navigating their dedicated opt-out search
page (behind Cloudflare), searching by name, selecting a record, solving
CAPTCHAs, and providing an email for verification.  The page is slow to load
and may need a reload.  This plugin opens a visible browser and automates
form filling, but the user must solve Cloudflare/CAPTCHA challenges manually.
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
PAGE_TIMEOUT_MS = 60_000  # 60s — page is notoriously slow
CLOUDFLARE_TIMEOUT_MS = 120_000  # 2 min for Cloudflare challenge
EMAIL_VERIFY_TIMEOUT = 300.0  # 5 min for user to check email
SEARCH_DELAY_SECONDS = 3

# Profile URLs: /people/First-Last/State/P<id>
_PROFILE_URL_RE = re.compile(r"/people/[A-Za-z-]+/[A-Za-z]+/P[a-zA-Z0-9]+")

# Captcha detection (Cloudflare Turnstile + reCAPTCHA)
_CAPTCHA_INDICATOR_SEL = (
    "iframe[src*='challenge'], div.cf-turnstile, "
    "iframe[src*='recaptcha'], div.g-recaptcha"
)


# ---------------------------------------------------------------------------
# Pure helper functions (testable without browser)
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for BeenVerified URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_url(first: str, last: str, state: str = "") -> str:
    """Build a BeenVerified people search URL (used for profile URL matching).

    >>> _build_search_url("Jane", "Smith", "Illinois")
    'https://www.beenverified.com/people/Jane-Smith/Illinois/'
    """
    name_slug = _normalize_name(f"{first} {last}")
    url = f"{BASE_URL}/people/{name_slug}"
    if state:
        url += f"/{_normalize_name(state)}"
    return url + "/"


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate BeenVerified people search URLs from all profile search variants."""
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
# Browser helpers
# ---------------------------------------------------------------------------


async def _handle_captcha(page: Page, description: str = "page") -> bool:
    return await wait_for_captcha(
        page, description=description, selector=_CAPTCHA_INDICATOR_SEL
    )


async def _navigate_opt_out_page(page: Page) -> bool:
    """Navigate to BeenVerified opt-out page and wait for Cloudflare to clear.

    Returns True if the search form loaded successfully.
    """
    try:
        await page.goto(OPT_OUT_URL, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception:
        log.info("Slow loading opt-out page, continuing...")

    # Wait for Cloudflare challenge to resolve
    try:
        cf = page.locator("text=/Performing security verification|Just a moment/i")
        if await cf.count() > 0:
            log.info("Cloudflare challenge — please complete it in the browser")
            await cf.first.wait_for(state="hidden", timeout=CLOUDFLARE_TIMEOUT_MS)
            await asyncio.sleep(3)
    except Exception:
        log.warning("Cloudflare challenge may still be active, continuing...")

    # Verify the search form loaded
    try:
        await page.wait_for_selector('button:has-text("Search")', timeout=PAGE_TIMEOUT_MS)
        return True
    except Exception:
        log.error("Search form did not load — try reloading the page")
        return False


async def _fill_search_form(
    page: Page, first_name: str, last_name: str, state: str = ""
) -> None:
    """Fill the opt-out search form fields."""
    first_input = page.get_by_role("textbox", name="First Name")
    await first_input.click()
    await first_input.fill("")
    await first_input.press_sequentially(first_name, delay=20)

    last_input = page.get_by_role("textbox", name="Last Name")
    await last_input.click()
    await last_input.fill("")
    await last_input.press_sequentially(last_name, delay=20)

    if state:
        try:
            state_combo = page.get_by_role("combobox").first
            await state_combo.click()
            await asyncio.sleep(0.5)
            # Type state to filter the dropdown options
            state_input = page.locator("[class*='select'] input, [role='combobox'] + input").first
            await state_input.press_sequentially(state, delay=50)
            await asyncio.sleep(0.5)
            # Click first matching option
            option = page.get_by_role("option").first
            if await option.count() > 0:
                await option.click()
                await asyncio.sleep(0.3)
        except Exception:
            log.debug("Could not select state %s from dropdown", state)


async def _click_search_and_wait(page: Page) -> bool:
    """Click Search and wait for results to appear."""
    try:
        search_btn = page.locator('button:has-text("Search")').first
        await search_btn.click(timeout=10_000)
    except Exception:
        log.error("Could not click Search button")
        return False

    # Wait for page to load results
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    except Exception:
        pass
    await asyncio.sleep(5)
    return True


async def _extract_opt_out_results(page: Page, profile: Profile) -> list[Listing]:
    """Extract result records from the opt-out search results page."""
    listings: list[Listing] = []

    # BeenVerified opt-out results use various card structures; try several selectors
    card_selectors = [
        "[class*='person-card']",
        "[class*='people-card']",
        "[class*='record-card']",
        "[class*='optout'] [class*='card']",
        "[class*='result'] li",
        "li[class*='person']",
    ]

    cards = []
    for sel in card_selectors:
        cards = await page.query_selector_all(sel)
        if cards:
            log.debug("Found %d result cards with selector: %s", len(cards), sel)
            break

    if not cards:
        # Fallback: look for any clickable opt-out links/buttons
        opt_out_els = await page.query_selector_all(
            'a:has-text("opt out"), button:has-text("opt out"), '
            'a:has-text("Opt Out"), button:has-text("Opt Out")'
        )
        if opt_out_els:
            log.info("Found %d opt-out buttons (no card structure detected)", len(opt_out_els))
            for el in opt_out_els:
                # Get surrounding text for context
                parent = await el.evaluate_handle("el => el.closest('li, div, tr') || el.parentElement")
                text = await parent.evaluate("el => el.textContent") if parent else ""
                lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
                found_name = lines[0] if lines else ""
                found_location = ""
                for line in lines[1:]:
                    if "," in line or re.search(r"\b[A-Z]{2}\b", line):
                        found_location = line
                        break

                confidence = compute_confidence(profile, found_name, found_location, "")
                if confidence > 0.1:
                    listings.append(
                        Listing(
                            broker_id="beenverified",
                            profile_id=profile.id,
                            url=OPT_OUT_URL,
                            found_name=found_name,
                            found_location=found_location,
                            confidence=confidence,
                        )
                    )
            return listings

        log.debug("No result cards found on opt-out page")
        return []

    for card in cards:
        try:
            text = (await card.inner_text()).strip()
            if not text:
                continue

            # Try to find a link
            link = await card.query_selector("a[href*='/people/']")
            href = ""
            if link:
                href = (await link.get_attribute("href")) or ""
            if href and not href.startswith("http"):
                href = f"{BASE_URL}{href}"

            # Parse name and location from card text
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            found_name = lines[0] if lines else ""
            found_location = ""
            found_age = ""
            for line in lines[1:]:
                if re.search(r"\d+\s*(years|yrs|yr|age)", line, re.IGNORECASE) or re.match(
                    r"^Age\s*\d+", line
                ):
                    found_age = line
                elif not found_location and ("," in line or re.search(r"\b[A-Z]{2}\b", line)):
                    found_location = line

            confidence = compute_confidence(profile, found_name, found_location, found_age)

            if confidence > 0.1:
                listings.append(
                    Listing(
                        broker_id="beenverified",
                        profile_id=profile.id,
                        url=href or OPT_OUT_URL,
                        found_name=found_name,
                        found_location=found_location,
                        found_age=found_age,
                        confidence=confidence,
                    )
                )
        except Exception:
            log.debug("Failed to extract card data")
            continue

    return listings


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
            difficulty=Difficulty.MEDIUM,
            expected_days=2,
            recheck_days=90,
            requires_interaction=True,
            notes=(
                "Slow-loading opt-out page behind Cloudflare. "
                "User must solve Cloudflare/CAPTCHA challenges. "
                "Search by name, select record, enter email, verify via email link."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search BeenVerified's opt-out page for matching records.

        Opens a visible browser since Cloudflare challenges require user
        interaction.  The opt-out search is the only free way to check
        for records on BeenVerified (main search requires membership).
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping BeenVerified search")
            return []

        listings: list[Listing] = []

        async with stealth_playwright() as pw:
            browser = await launch_browser(pw, headless=False)
            try:
                page: Page = await browser.new_page(user_agent=USER_AGENT)

                if not await _navigate_opt_out_page(page):
                    return []

                for i, variant in enumerate(profile.search_variants()):
                    first = variant.get("first_name", "")
                    last = variant.get("last_name", "")
                    if not first or not last:
                        continue

                    if i > 0:
                        await asyncio.sleep(SEARCH_DELAY_SECONDS)
                        if not await _navigate_opt_out_page(page):
                            continue

                    state = variant.get("state", "")
                    await _fill_search_form(page, first, last, state)

                    log.info(
                        "Search form filled for %s %s — solve CAPTCHA if present",
                        first,
                        last,
                    )

                    if not await _handle_captcha(page, description="opt-out search"):
                        log.warning("Captcha not resolved, skipping this search")
                        continue

                    if not await _click_search_and_wait(page):
                        continue

                    # Handle post-search captcha
                    await _handle_captcha(page, description="search results")

                    batch = await _extract_opt_out_results(page, profile)
                    listings.extend(batch)

                    if listings:
                        break  # Got results, stop searching variants
            finally:
                await browser.close()

        return deduplicate(listings)

    async def submit_opt_out(self, listing: Listing) -> bool:
        """Submit an opt-out via BeenVerified's opt-out search page.

        Flow:
          1. Navigate to opt-out page (handle Cloudflare)
          2. Fill name/state and search
          3. Find and click opt-out on matching record
          4. Enter email address
          5. Wait for "check your email" confirmation

        User must solve Cloudflare/CAPTCHA challenges and verify via email.
        """
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False

        # Get email and profile info for the form
        email = ""
        first_name = ""
        last_name = ""
        state = ""
        try:
            from dataremoval.core.database import Database

            db = Database()
            profile = db.get_profile(listing.profile_id)
            if profile:
                if profile.email_addresses:
                    email = profile.email_addresses[0]
                first_name = profile.first_name
                last_name = profile.last_name
                if profile.addresses:
                    state = profile.addresses[0].state
        except Exception:
            log.debug("Could not load profile data")

        # Fall back to parsing name from listing
        if not first_name or not last_name:
            parts = listing.found_name.split()
            first_name = parts[0] if parts else ""
            last_name = parts[-1] if len(parts) > 1 else ""

        # Extract state from listing location
        if not state and listing.found_location:
            state_match = re.search(r"\b([A-Z]{2})\b", listing.found_location)
            if state_match:
                state = state_match.group(1)

        if not first_name or not last_name:
            log.error("No name available for BeenVerified opt-out search")
            return False

        try:
            async with stealth_playwright() as pw:
                browser = await launch_browser(pw, headless=False)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)

                    if not await _navigate_opt_out_page(page):
                        return False

                    # Fill and submit search
                    await _fill_search_form(page, first_name, last_name, state)

                    log.info(
                        "Search form filled for %s %s — solve CAPTCHA and click Search",
                        first_name,
                        last_name,
                    )

                    if not await _handle_captcha(page, description="opt-out search"):
                        log.warning("Captcha not resolved")

                    if not await _click_search_and_wait(page):
                        return False

                    await _handle_captcha(page, description="search results")

                    # Try to find and click the opt-out button for matching record
                    try:
                        opt_out_btns = page.locator(
                            'button:has-text("opt out"), button:has-text("Opt Out"), '
                            'button:has-text("Opt-Out"), a:has-text("opt out"), '
                            'a:has-text("Opt Out"), a:has-text("Opt-Out")'
                        )
                        count = await opt_out_btns.count()
                        if count > 0:
                            await opt_out_btns.first.click(timeout=10_000)
                            log.info("Clicked opt-out button")
                            await asyncio.sleep(3)
                        else:
                            log.info(
                                "No opt-out button found automatically — "
                                "please select your record in the browser"
                            )
                    except Exception:
                        log.info("Please select your record and click opt-out in the browser")

                    # Handle any additional captcha after clicking opt-out
                    await _handle_captcha(page, description="opt-out confirmation")

                    # Try to auto-fill email if we have it
                    if email:
                        try:
                            email_input = page.locator(
                                'input[type="email"], '
                                'input[placeholder*="email" i], '
                                'input[name*="email" i]'
                            ).first
                            await email_input.click()
                            await email_input.press_sequentially(email, delay=20)
                            log.info("Auto-filled email: %s", email)
                        except Exception:
                            log.debug("Could not auto-fill email")

                    # Try to click submit/send button
                    try:
                        submit_btn = page.locator(
                            'button:has-text("Send"), '
                            'button:has-text("Submit"), '
                            'button:has-text("Verify"), '
                            'button[type="submit"]'
                        ).first
                        if await submit_btn.is_visible():
                            await submit_btn.click(timeout=10_000)
                            await asyncio.sleep(2)
                    except Exception:
                        log.debug("Could not auto-click submit")

                    log.warning(
                        "Check your email (%s) for the BeenVerified verification link. "
                        "Click the link to complete the opt-out. "
                        "Waiting up to %d seconds...",
                        email or "your email address",
                        int(EMAIL_VERIFY_TIMEOUT),
                    )

                    # Wait for confirmation text
                    try:
                        await page.wait_for_selector(
                            "text=/check your email|verification.*sent|"
                            "email.*sent|opt.?out.*submitted|"
                            "request.*received|confirmation/i",
                            timeout=EMAIL_VERIFY_TIMEOUT * 1000,
                        )
                        log.info("Opt-out submitted for %s", listing.found_name)
                        return True
                    except Exception:
                        log.error(
                            "Could not confirm opt-out submission for %s",
                            listing.found_name,
                        )
                        return False

                finally:
                    await browser.close()

        except Exception:
            log.exception("Opt-out submission failed for %s", listing.url)
            return False

    async def check_status(self, listing: Listing) -> str:
        """Check whether a listing URL still resolves (httpx, no browser)."""
        return await check_url_status(listing.url)


register_broker(BeenVerifiedPlugin())

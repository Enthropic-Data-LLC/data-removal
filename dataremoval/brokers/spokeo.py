"""Spokeo broker plugin.

Opt-out: Email + online form
Difficulty: Medium
Expected time: 72h

Spokeo is a React/Next.js SPA — pages require JavaScript rendering.
The opt-out form uses reCAPTCHA v3 and CSRF tokens, so Playwright is
required for search and opt-out while httpx suffices for status checks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

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
)
from dataremoval.core.models import Listing, Profile

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page

if HAS_PLAYWRIGHT:
    from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

BASE_URL = "https://www.spokeo.com"
OPT_OUT_URL = f"{BASE_URL}/optout"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 20_000
SEARCH_DELAY_SECONDS = 2

# Profile links look like /David-Brown/North-Carolina/Charlotte/p3795149494909224758404708
# We match the /p<digits> suffix to distinguish from /people/, /privacy, etc.
_PROFILE_LINK_SEL = "a[href*='/p']"
_PROFILE_URL_RE = re.compile(r"/p\d{5,}$")


def _is_profile_url(url: str) -> bool:
    """Return True if *url* looks like a Spokeo person profile page."""
    from urllib.parse import urlparse

    path = urlparse(url).path
    return bool(_PROFILE_URL_RE.search(path))


def _normalize_name(name: str) -> str:
    """Normalize a name for Spokeo URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate Spokeo search URLs from profile search variants."""
    urls: list[str] = []
    for variant in profile.search_variants():
        first = variant.get("first_name", "")
        last = variant.get("last_name", "")
        if not first or not last:
            continue
        slug = _normalize_name(f"{first} {last}")
        state = variant.get("state", "")
        if state:
            urls.append(f"{BASE_URL}/{slug}/{state}")
        else:
            urls.append(f"{BASE_URL}/{slug}")
    return urls


class SpokeoPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="spokeo",
            name="Spokeo",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.EMAIL,
            opt_out_url=OPT_OUT_URL,
            difficulty=Difficulty.MEDIUM,
            expected_days=3,
            recheck_days=90,
            notes="Submit listing URL at opt-out page, then confirm via email.",
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search Spokeo for listings matching *profile*."""
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping Spokeo search")
            return []

        urls = _build_search_urls(profile)
        if not urls:
            return []

        opt_out_email = profile.email_addresses[0] if profile.email_addresses else ""
        listings: list[Listing] = []

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(headless=True)
            try:
                page: Page = await browser.new_page(user_agent=USER_AGENT)
                for i, url in enumerate(urls):
                    if i > 0:
                        await asyncio.sleep(SEARCH_DELAY_SECONDS)
                    try:
                        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                        await page.wait_for_selector(_PROFILE_LINK_SEL, timeout=PAGE_TIMEOUT_MS)
                    except Exception:
                        log.debug("No results or timeout for %s", url)
                        continue

                    cards = await page.query_selector_all(_PROFILE_LINK_SEL)
                    for card in cards:
                        href = await card.get_attribute("href") or ""
                        if not href.startswith("http"):
                            href = f"{BASE_URL}{href}"

                        if not _is_profile_url(href):
                            continue

                        text = (await card.inner_text()).strip()
                        lines = [s.strip() for s in text.splitlines() if s.strip()]

                        found_name = lines[0] if lines else ""
                        found_location = lines[1] if len(lines) > 1 else ""
                        found_age = ""
                        for line in lines:
                            if line.isdigit() and 10 < int(line) < 120:
                                found_age = line
                                break

                        confidence = compute_confidence(
                            profile, found_name, found_location, found_age
                        )
                        listings.append(
                            Listing(
                                broker_id="spokeo",
                                profile_id=profile.id,
                                url=href,
                                found_name=found_name,
                                found_location=found_location,
                                found_age=found_age,
                                confidence=confidence,
                                raw_data={"opt_out_email": opt_out_email},
                            )
                        )
            finally:
                await browser.close()

        return deduplicate(listings)

    async def submit_opt_out(self, listing: Listing) -> bool:
        """Submit an opt-out request for *listing* via Spokeo's opt-out page."""
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False

        email = listing.raw_data.get("opt_out_email", "")
        if not email:
            log.warning("No opt-out email for listing %s", listing.url)
            return False

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)
                    await page.goto(
                        OPT_OUT_URL, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded"
                    )

                    # Fill URL and email fields
                    url_input = page.locator(
                        'input[name="url"], input[placeholder*="spokeo.com"]'
                    ).first
                    await url_input.fill(listing.url)

                    email_input = page.locator('input[name="email"], input[type="email"]').first
                    await email_input.fill(email)

                    # Click submit
                    submit_btn = page.locator(
                        'button:has-text("OPT OUT"), button[type="submit"]'
                    ).first
                    await submit_btn.click()

                    # Wait for confirmation
                    await page.wait_for_selector(
                        "text=/check your email|confirmation|opt.?out.*submitted/i",
                        timeout=PAGE_TIMEOUT_MS,
                    )
                    return True
                finally:
                    await browser.close()
        except Exception:
            log.exception("Opt-out submission failed for %s", listing.url)
            return False

    async def check_status(self, listing: Listing) -> str:
        """Check whether a listing URL still resolves (httpx, no browser needed)."""
        return await check_url_status(listing.url, treat_403_as_unknown=False)


register_broker(SpokeoPlugin())

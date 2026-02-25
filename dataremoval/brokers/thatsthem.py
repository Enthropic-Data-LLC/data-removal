"""ThatsThem broker plugin.

Opt-out: Online form at https://thatsthem.com/optout
Difficulty: Easy
Expected time: 5-14 days

ThatsThem provides people search by name and location.  The opt-out
form requires name, email, and address.  Standard CAPTCHA protection.
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

BASE_URL = "https://thatsthem.com"
OPT_OUT_URL = f"{BASE_URL}/optout"
USER_AGENT = DEFAULT_USER_AGENT
PAGE_TIMEOUT_MS = 30_000
SEARCH_DELAY_SECONDS = 3

_RESULT_CARD_SEL = "div.ThatsThem-people-record"
_RESULT_LINK_SEL = "a.name-link"
_RESULT_NAME_SEL = ".name-link"
_RESULT_LOCATION_SEL = ".location"
_RESULT_AGE_SEL = ".age"

_PROFILE_URL_RE = re.compile(r"/name/[A-Za-z-]+/")
_CAPTCHA_SEL = (
    "iframe[src*='recaptcha'], div.g-recaptcha, iframe[src*='challenge'], div.cf-turnstile"
)


def _normalize_name(name: str) -> str:
    """Normalize a name for ThatsThem URL format: ``'Jane Doe' -> 'Jane-Doe'``."""
    return "-".join(name.split())


def _build_search_url(first: str, last: str, city: str = "", state: str = "") -> str:
    """Build a ThatsThem search URL.

    >>> _build_search_url("Jane", "Smith", "Springfield", "IL")
    'https://thatsthem.com/name/Jane-Smith/Springfield-IL'
    """
    name_slug = _normalize_name(f"{first} {last}")
    url = f"{BASE_URL}/name/{name_slug}"
    location = f"{city} {state}".strip()
    if location:
        url += f"/{_normalize_name(location)}"
    return url


def _build_search_urls(profile: Profile) -> list[str]:
    """Generate ThatsThem search URLs from all profile search variants."""
    urls: list[str] = []
    for variant in profile.search_variants():
        first, last = variant.get("first_name", ""), variant.get("last_name", "")
        if not first or not last:
            continue
        urls.append(
            _build_search_url(
                first,
                last,
                variant.get("city", ""),
                variant.get("state", ""),
            )
        )
    return urls


def _is_profile_url(url: str) -> bool:
    """Return True if *url* looks like a ThatsThem person profile page."""
    return bool(_PROFILE_URL_RE.search(urlparse(url).path))


class ThatsThemPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="thatsthem",
            name="ThatsThem",
            url=BASE_URL,
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url=OPT_OUT_URL,
            difficulty=Difficulty.EASY,
            expected_days=14,
            recheck_days=90,
            notes=(
                "Opt-out form requires name, email, and address. "
                "Standard CAPTCHA. 5-14 days processing."
            ),
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search ThatsThem for listings matching *profile*."""
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — skipping ThatsThem search")
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

                    if not await wait_for_captcha(page, description=url, selector=_CAPTCHA_SEL):
                        log.warning("Skipping %s due to unsolved captcha", url)
                        continue
                    try:
                        await page.wait_for_selector(_RESULT_CARD_SEL, timeout=PAGE_TIMEOUT_MS)
                    except Exception:
                        log.debug("No result cards found for %s", url)
                        continue

                    for card in await page.query_selector_all(_RESULT_CARD_SEL):
                        try:
                            link_el = await card.query_selector(_RESULT_LINK_SEL)
                            href = (await link_el.get_attribute("href") or "") if link_el else ""
                            if href and not href.startswith("http"):
                                href = f"{BASE_URL}{href}"
                            if not _is_profile_url(href):
                                continue
                            name_el = await card.query_selector(_RESULT_NAME_SEL)
                            found_name = (await name_el.inner_text()).strip() if name_el else ""
                            loc_el = await card.query_selector(_RESULT_LOCATION_SEL)
                            found_location = (await loc_el.inner_text()).strip() if loc_el else ""
                            age_el = await card.query_selector(_RESULT_AGE_SEL)
                            found_age = (await age_el.inner_text()).strip() if age_el else ""
                            confidence = compute_confidence(
                                profile, found_name, found_location, found_age
                            )
                            listings.append(
                                Listing(
                                    broker_id="thatsthem",
                                    profile_id=profile.id,
                                    url=href,
                                    found_name=found_name,
                                    found_location=found_location,
                                    found_age=found_age,
                                    confidence=confidence,
                                )
                            )
                        except Exception:
                            log.debug("Failed to extract card on %s", url)
            finally:
                await browser.close()
        return deduplicate(listings)

    async def submit_opt_out(self, listing: Listing) -> bool:
        """Submit an opt-out request via ThatsThem's opt-out form."""
        if not HAS_PLAYWRIGHT:
            log.warning("Playwright not installed — cannot submit opt-out")
            return False
        try:
            async with stealth_playwright() as pw:
                browser = await launch_browser(pw, headless=False)
                try:
                    page = await browser.new_page(user_agent=USER_AGENT)
                    await page.goto(
                        OPT_OUT_URL, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded"
                    )
                    if not await wait_for_captcha(
                        page, description="opt-out page", selector=_CAPTCHA_SEL
                    ):
                        log.error("Could not pass captcha on opt-out page")
                        return False
                    # Fill name and email fields
                    name_input = page.locator(
                        'input[name="name"], input[placeholder*="name" i]'
                    ).first
                    await name_input.fill(listing.found_name)
                    email_input = page.locator('input[name="email"], input[type="email"]').first
                    await email_input.fill(listing.raw_data.get("opt_out_email", ""))
                    # Submit
                    submit_btn = page.locator(
                        'button[type="submit"], input[type="submit"], '
                        'button:has-text("Submit"), button:has-text("Opt Out")'
                    ).first
                    await submit_btn.click(timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_selector(
                        "text=/submitted|success|request.*received|opt.?out.*complete/i",
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


register_broker(ThatsThemPlugin())

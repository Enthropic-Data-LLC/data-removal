"""Shared utilities for broker plugins.

Extracts common patterns used across multiple broker plugins:
- Confidence scoring for matching listings to profiles
- Listing deduplication
- Playwright stealth browser helpers
- Captcha detection and waiting
- HTTP status checking
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import httpx

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
# Shared constants
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

DEFAULT_CAPTCHA_SELECTOR = (
    "iframe[src*='challenge'], div.cf-turnstile, "
    "iframe[src*='recaptcha'], div.g-recaptcha, "
    "iframe[src*='captcha-delivery.com'], iframe[src*='geo.captcha']"
)

DEFAULT_PAGE_TIMEOUT_MS = 30_000
DEFAULT_CAPTCHA_POLL_INTERVAL = 2.0
DEFAULT_CAPTCHA_TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------


def compute_confidence(
    profile: Profile,
    found_name: str,
    found_location: str,
    found_age: str,
    found_relatives: list[str] | None = None,
) -> float:
    """Compute a 0-1 confidence score for a listing match.

    Scoring breakdown:
      - Name match: +0.4 (exact) or +0.3 (partial)
      - State match: +0.2
      - City match: +0.15
      - Age match: +0.15 (within 1 year, handles "Age 35" format)
      - Relatives: up to +0.1
    """
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

    # Age match (+0.15) — handles "35", "Age 35", etc.
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


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def deduplicate(listings: list[Listing]) -> list[Listing]:
    """Remove duplicate listings by URL."""
    seen: set[str] = set()
    unique: list[Listing] = []
    for listing in listings:
        if listing.url not in seen:
            seen.add(listing.url)
            unique.append(listing)
    return unique


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


def stealth_playwright():
    """Return a stealth-wrapped async_playwright context manager if available."""
    pw_ctx = async_playwright()
    if HAS_STEALTH:
        return Stealth().use_async(pw_ctx)
    return pw_ctx


async def launch_browser(
    pw: Playwright, *, headless: bool = True, extra_args: list[str] | None = None
) -> Browser:
    """Launch Chromium with stealth args."""
    args = list(STEALTH_ARGS)
    if extra_args:
        args.extend(extra_args)
    return await pw.chromium.launch(headless=headless, args=args)


# ---------------------------------------------------------------------------
# Captcha helpers
# ---------------------------------------------------------------------------


async def is_captcha_page(
    page: Page,
    selector: str = DEFAULT_CAPTCHA_SELECTOR,
    url_fragment: str | None = None,
) -> bool:
    """Detect whether the current page shows a captcha challenge."""
    if url_fragment and url_fragment in page.url:
        return True
    indicator = await page.query_selector(selector)
    return indicator is not None


async def wait_for_captcha(
    page: Page,
    description: str = "page",
    selector: str = DEFAULT_CAPTCHA_SELECTOR,
    url_fragment: str | None = None,
    timeout: float = DEFAULT_CAPTCHA_TIMEOUT,
    poll_interval: float = DEFAULT_CAPTCHA_POLL_INTERVAL,
) -> bool:
    """If page shows a captcha, wait for the user to solve it manually.

    Returns True if the captcha was solved (or there was no captcha).
    Returns False if the timeout expired.
    """
    if not await is_captcha_page(page, selector=selector, url_fragment=url_fragment):
        return True

    log.warning(
        "Captcha detected on %s. Waiting up to %d seconds for resolution...",
        description,
        int(timeout),
    )

    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        if not await is_captcha_page(page, selector=selector, url_fragment=url_fragment):
            log.info("Captcha cleared. Continuing automation.")
            await page.wait_for_load_state("domcontentloaded")
            return True

    log.error("Captcha timeout after %d seconds on %s", int(timeout), description)
    return False


# ---------------------------------------------------------------------------
# HTTP status checking
# ---------------------------------------------------------------------------


async def check_url_status(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    treat_403_as_unknown: bool = True,
) -> str:
    """Check whether a URL still resolves.

    Returns 'removed', 'still_listed', or 'unknown'.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        ) as client:
            resp = await client.get(url, timeout=10)
            if resp.status_code in (404, 410):
                return "removed"
            if resp.status_code == 403 and treat_403_as_unknown:
                log.debug("Got 403 for %s — possible WAF block, status unknown", url)
                return "unknown"
            return "still_listed"
    except Exception:
        return "unknown"

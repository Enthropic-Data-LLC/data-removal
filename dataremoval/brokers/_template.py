"""
TEMPLATE: Copy this file to add a new broker plugin.

Steps:
  1. Copy to dataremoval/brokers/{broker_slug}.py
  2. Rename the class
  3. Fill in info(), search(), submit_opt_out()
  4. The plugin auto-registers on import via discover()

That's it — the CLI, database, and state machine all work automatically.
"""

from __future__ import annotations

from dataremoval.brokers import (
    BrokerInfo,
    BrokerPlugin,
    Difficulty,
    OptOutMethod,
)
from dataremoval.brokers._utils import (  # noqa: F401
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


class _TemplatePlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="template",  # unique slug
            name="Template Site",  # display name
            url="https://example.com",  # homepage
            category="people_search",  # people_search | background | marketing
            opt_out_method=OptOutMethod.ONLINE_FORM,  # ONLINE_FORM | EMAIL | MAIL | ACCOUNT | API
            opt_out_url="https://example.com/optout",  # direct link to opt-out
            difficulty=Difficulty.MEDIUM,  # EASY | MEDIUM | HARD
            expected_days=3,  # typical removal time
            recheck_days=90,  # monitoring interval
            notes="",
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """
        Search the site for listings matching the profile.

        Typical approach:
          - Use profile.search_variants() for name/location combos
          - Make HTTP requests (httpx) or use browser automation (playwright)
          - Parse results and return Listing objects

        Each Listing needs at minimum:
          - broker_id: self.id
          - profile_id: profile.id
          - url: direct link to the listing
        """
        return []

    async def submit_opt_out(self, listing: Listing) -> bool:
        """
        Submit a removal request for this listing.

        Return True if the submission appeared to succeed.
        """
        return False

    async def check_status(self, listing: Listing) -> str:
        """
        Re-check if a listing has been removed.
        Return 'removed', 'still_listed', or 'unknown'.
        """
        return "unknown"


# NOTE: Don't register the template — uncomment when using as a real plugin
# register_broker(_TemplatePlugin())

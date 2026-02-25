"""TruePeopleSearch broker plugin.

Opt-out: Online form at https://www.truepeoplesearch.com/removal
Difficulty: Easy
Expected time: 24h
"""

from __future__ import annotations

import httpx

from dataremoval.brokers import (
    BrokerInfo,
    BrokerPlugin,
    Difficulty,
    OptOutMethod,
    register_broker,
)
from dataremoval.core.models import Listing, Profile


class TruePeopleSearchPlugin(BrokerPlugin):

    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="truepeoplesearch",
            name="TruePeopleSearch",
            url="https://www.truepeoplesearch.com",
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url="https://www.truepeoplesearch.com/removal",
            difficulty=Difficulty.EASY,
            expected_days=1,
            recheck_days=90,
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """
        Search TruePeopleSearch for listings.

        In production this would use httpx + parsing or a headless browser.
        For now, returns a placeholder to show the pattern.
        """
        listings: list[Listing] = []

        for variant in profile.search_variants():
            first = variant.get("first_name", "")
            last = variant.get("last_name", "")
            state = variant.get("state", "")
            city = variant.get("city", "")

            # TODO: Actual HTTP request + HTML parsing
            # search_url = f"{self.info().url}/results"
            # params = {"name": f"{first} {last}", "citystatezip": f"{city} {state}"}
            # async with httpx.AsyncClient() as client:
            #     resp = await client.get(search_url, params=params)
            #     ... parse response ...

            # Placeholder: in a real implementation, parse the results page
            # and yield Listing objects for each matching record.
            pass

        return listings

    async def submit_opt_out(self, listing: Listing) -> bool:
        """
        Submit removal via the online form.

        Steps:
          1. Navigate to removal page
          2. Search for the listing
          3. Click "Remove This Record"
          4. Return success
        """
        # TODO: Actual form submission
        # async with httpx.AsyncClient() as client:
        #     resp = await client.post(
        #         self.info().opt_out_url,
        #         data={"recordUrl": listing.url},
        #     )
        #     return resp.status_code == 200
        return False

    async def check_status(self, listing: Listing) -> str:
        """Check if the listing URL still resolves."""
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(listing.url, timeout=10)
                if resp.status_code == 404:
                    return "removed"
                return "still_listed"
        except Exception:
            return "unknown"


# Auto-register on import
register_broker(TruePeopleSearchPlugin())

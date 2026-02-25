"""Whitepages broker plugin.

Opt-out: Online form + email verification
Difficulty: Easy
Expected time: 48h
"""

from __future__ import annotations

from dataremoval.brokers import (
    BrokerInfo,
    BrokerPlugin,
    Difficulty,
    OptOutMethod,
    register_broker,
)
from dataremoval.core.models import Listing, Profile


class WhitepagesPlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="whitepages",
            name="Whitepages",
            url="https://www.whitepages.com",
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url="https://www.whitepages.com/suppression-requests",
            difficulty=Difficulty.EASY,
            expected_days=2,
            recheck_days=90,
            notes="Requires email verification. Re-lists quarterly from public records.",
        )

    async def search(self, profile: Profile) -> list[Listing]:
        # TODO: implement search
        # URL pattern: /name/{First}-{Last}/{City}-{State}
        return []

    async def submit_opt_out(self, listing: Listing) -> bool:
        # TODO: implement opt-out
        # 1. POST to suppression-requests with listing URL
        # 2. Receive verification email
        # 3. Click verification link (requires email integration)
        return False


register_broker(WhitepagesPlugin())

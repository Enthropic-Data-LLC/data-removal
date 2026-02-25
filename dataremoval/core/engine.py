"""Orchestration engine — coordinates brokers, state transitions, and scheduling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from dataremoval.brokers import BrokerPlugin, registry
from dataremoval.core.database import Database
from dataremoval.core.models import (
    Listing,
    Profile,
    RemovalRequest,
    RemovalState,
)


class Engine:
    """
    High-level operations that coordinate broker plugins with the database.

    The CLI commands call engine methods; the engine calls broker plugins
    and persists state.
    """

    def __init__(self, db: Database, on_event: Callable | None = None):
        self.db = db
        self.on_event = on_event or (lambda *a: None)

    # ------------------------------------------------------------------
    # Scan: search all (or specific) brokers for a profile
    # ------------------------------------------------------------------

    async def scan(
        self,
        profile: Profile,
        broker_ids: list[str] | None = None,
        concurrency: int = 5,
    ) -> list[Listing]:
        """Search broker sites for listings matching a profile."""
        plugins = self._resolve_plugins(broker_ids)
        all_listings: list[Listing] = []
        sem = asyncio.Semaphore(concurrency)

        async def _search_one(plugin: BrokerPlugin) -> list[Listing]:
            async with sem:
                self.on_event("scan_start", plugin.id)
                try:
                    results = await plugin.search(profile)
                    for listing in results:
                        self.db.save_listing(listing)
                        # Create a removal request in DISCOVERED state
                        req = RemovalRequest(
                            listing_id=listing.id,
                            broker_id=listing.broker_id,
                            profile_id=profile.id,
                        )
                        self.db.save_request(req)
                    self.on_event("scan_done", plugin.id, len(results))
                    return results
                except Exception as e:
                    self.on_event("scan_error", plugin.id, str(e))
                    return []

        tasks = [_search_one(p) for p in plugins]
        results = await asyncio.gather(*tasks)
        for batch in results:
            all_listings.extend(batch)

        return all_listings

    # ------------------------------------------------------------------
    # Remove: submit opt-outs for discovered listings
    # ------------------------------------------------------------------

    async def remove(
        self,
        profile_id: str,
        broker_ids: list[str] | None = None,
        concurrency: int = 3,
    ) -> dict[str, int]:
        """Submit opt-out requests for all DISCOVERED or FAILED listings."""
        requests = self.db.get_requests(profile_id=profile_id)
        targets = [
            r
            for r in requests
            if r.state in (RemovalState.DISCOVERED, RemovalState.FAILED, RemovalState.RE_LISTED)
            and (broker_ids is None or r.broker_id in broker_ids)
        ]

        results = {"submitted": 0, "failed": 0, "skipped": 0}
        sem = asyncio.Semaphore(concurrency)

        async def _submit_one(req: RemovalRequest) -> None:
            async with sem:
                plugin = registry.get(req.broker_id)
                if not plugin:
                    results["skipped"] += 1
                    return

                listing = self._get_listing(req.listing_id)
                if not listing:
                    results["skipped"] += 1
                    return

                self.on_event("remove_start", req.broker_id, listing.url)
                try:
                    success = await plugin.submit_opt_out(listing)
                    if success:
                        req.transition(RemovalState.SUBMITTED, "opt-out submitted")
                        results["submitted"] += 1
                    else:
                        req.transition(RemovalState.FAILED, "submission returned false")
                        results["failed"] += 1
                except Exception as e:
                    req.transition(RemovalState.FAILED, str(e))
                    results["failed"] += 1

                self.db.save_request(req)
                self.on_event("remove_done", req.broker_id, req.state.value)

        await asyncio.gather(*[_submit_one(r) for r in targets])
        return results

    # ------------------------------------------------------------------
    # Monitor: re-check confirmed removals for re-listings
    # ------------------------------------------------------------------

    async def monitor(
        self,
        profile_id: str,
        broker_ids: list[str] | None = None,
    ) -> dict[str, int]:
        """Check status of monitoring/confirmed requests."""
        requests = self.db.get_requests(profile_id=profile_id)
        targets = [
            r
            for r in requests
            if r.state in (RemovalState.CONFIRMED, RemovalState.MONITORING)
            and (broker_ids is None or r.broker_id in broker_ids)
        ]

        results = {"checked": 0, "still_removed": 0, "re_listed": 0, "unknown": 0}

        for req in targets:
            plugin = registry.get(req.broker_id)
            listing = self._get_listing(req.listing_id)
            if not plugin or not listing:
                continue

            self.on_event("monitor_check", req.broker_id)
            try:
                status = await plugin.check_status(listing)
                results["checked"] += 1

                if status == "removed":
                    if req.state == RemovalState.CONFIRMED:
                        req.transition(RemovalState.MONITORING, "verified removed")
                    req.last_checked_at = datetime.now().isoformat()
                    results["still_removed"] += 1

                elif status == "still_listed":
                    req.transition(RemovalState.RE_LISTED, "data reappeared")
                    results["re_listed"] += 1

                else:
                    results["unknown"] += 1

                self.db.save_request(req)
            except Exception as e:
                self.on_event("monitor_error", req.broker_id, str(e))
                results["unknown"] += 1

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_plugins(self, broker_ids: list[str] | None) -> list[BrokerPlugin]:
        if broker_ids:
            plugins = [registry.get(bid) for bid in broker_ids]
            return [p for p in plugins if p is not None]
        return registry.all()

    def _get_listing(self, listing_id: str) -> Listing | None:
        rows = self.db.get_listings()
        for listing in rows:
            if listing.id == listing_id:
                return listing
        return None

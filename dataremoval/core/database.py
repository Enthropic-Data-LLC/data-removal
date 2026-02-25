"""Local SQLite database for profiles, listings, and removal requests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from platformdirs import user_data_dir

from dataremoval.core.models import (
    Listing,
    Profile,
    RemovalRequest,
    RemovalState,
)

APP_NAME = "data-removal"
APP_AUTHOR = "data-removal"


def default_db_path() -> Path:
    """
    Cross-platform database path:
      Linux:   ~/.local/share/data-removal/data.db
      macOS:   ~/Library/Application Support/data-removal/data.db
      Windows: C:\\Users\\<user>\\AppData\\Local\\data-removal\\data.db
    """
    return Path(user_data_dir(APP_NAME, APP_AUTHOR, ensure_exists=True)) / "data.db"


class Database:
    """Thin wrapper around SQLite for local persistence."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id          TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS listings (
                id          TEXT PRIMARY KEY,
                broker_id   TEXT NOT NULL,
                profile_id  TEXT NOT NULL,
                data        TEXT NOT NULL,
                discovered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS removal_requests (
                id          TEXT PRIMARY KEY,
                listing_id  TEXT NOT NULL,
                broker_id   TEXT NOT NULL,
                profile_id  TEXT NOT NULL,
                state       TEXT NOT NULL,
                data        TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_listings_broker
                ON listings(broker_id);
            CREATE INDEX IF NOT EXISTS idx_listings_profile
                ON listings(profile_id);
            CREATE INDEX IF NOT EXISTS idx_removals_state
                ON removal_requests(state);
            CREATE INDEX IF NOT EXISTS idx_removals_profile
                ON removal_requests(profile_id);
        """)

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def save_profile(self, profile: Profile) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO profiles (id, data, created_at) VALUES (?, ?, ?)",
            (profile.id, profile.model_dump_json(), profile.created_at),
        )
        self.conn.commit()

    def get_profile(self, profile_id: str) -> Profile | None:
        row = self.conn.execute("SELECT data FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return Profile.model_validate_json(row["data"]) if row else None

    def list_profiles(self) -> list[Profile]:
        rows = self.conn.execute("SELECT data FROM profiles ORDER BY created_at").fetchall()
        return [Profile.model_validate_json(r["data"]) for r in rows]

    def delete_profile(self, profile_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def save_listing(self, listing: Listing) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO listings "
            "(id, broker_id, profile_id, data, discovered_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                listing.id,
                listing.broker_id,
                listing.profile_id,
                listing.model_dump_json(),
                listing.discovered_at,
            ),
        )
        self.conn.commit()

    def get_listings(
        self,
        profile_id: str | None = None,
        broker_id: str | None = None,
    ) -> list[Listing]:
        query = "SELECT data FROM listings WHERE 1=1"
        params: list = []
        if profile_id:
            query += " AND profile_id = ?"
            params.append(profile_id)
        if broker_id:
            query += " AND broker_id = ?"
            params.append(broker_id)
        query += " ORDER BY discovered_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [Listing.model_validate_json(r["data"]) for r in rows]

    # ------------------------------------------------------------------
    # Removal requests
    # ------------------------------------------------------------------

    def save_request(self, req: RemovalRequest) -> None:
        from datetime import datetime

        self.conn.execute(
            "INSERT OR REPLACE INTO removal_requests "
            "(id, listing_id, broker_id, profile_id, state, data, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                req.id,
                req.listing_id,
                req.broker_id,
                req.profile_id,
                req.state.value,
                req.model_dump_json(),
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    def get_requests(
        self,
        profile_id: str | None = None,
        broker_id: str | None = None,
        state: RemovalState | None = None,
    ) -> list[RemovalRequest]:
        query = "SELECT data FROM removal_requests WHERE 1=1"
        params: list = []
        if profile_id:
            query += " AND profile_id = ?"
            params.append(profile_id)
        if broker_id:
            query += " AND broker_id = ?"
            params.append(broker_id)
        if state:
            query += " AND state = ?"
            params.append(state.value)
        query += " ORDER BY updated_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [RemovalRequest.model_validate_json(r["data"]) for r in rows]

    def get_request(self, request_id: str) -> RemovalRequest | None:
        row = self.conn.execute(
            "SELECT data FROM removal_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return RemovalRequest.model_validate_json(row["data"]) if row else None

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self, profile_id: str | None = None) -> dict:
        where = "WHERE profile_id = ?" if profile_id else ""
        params = [profile_id] if profile_id else []

        listing_count = self.conn.execute(
            f"SELECT COUNT(*) as c FROM listings {where}", params
        ).fetchone()["c"]

        state_counts = {}
        rows = self.conn.execute(
            f"SELECT state, COUNT(*) as c FROM removal_requests {where} GROUP BY state",
            params,
        ).fetchall()
        for r in rows:
            state_counts[r["state"]] = r["c"]

        return {"listings": listing_count, "requests_by_state": state_counts}

    def close(self) -> None:
        self.conn.close()

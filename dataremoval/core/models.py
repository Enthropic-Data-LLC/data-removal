"""Core data models for the data removal framework."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# User profile — the canonical representation of "who am I"
# ---------------------------------------------------------------------------


class Address(BaseModel):
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    current: bool = False

    def __str__(self) -> str:
        parts = [p for p in (self.street, self.city, self.state, self.zip_code) if p]
        return ", ".join(parts)


class Profile(BaseModel):
    """Everything a broker site might know about a person."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    first_name: str
    last_name: str
    middle_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    date_of_birth: str = ""  # YYYY-MM-DD
    age: int | None = None
    addresses: list[Address] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)
    email_addresses: list[str] = Field(default_factory=list)
    relatives: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)

    def search_variants(self) -> list[dict]:
        """Generate search parameter combinations for broker lookups."""
        variants: list[dict] = []
        names = [{"first_name": self.first_name, "last_name": self.last_name}]
        for alias in self.aliases:
            parts = alias.split(maxsplit=1)
            if len(parts) == 2:
                names.append({"first_name": parts[0], "last_name": parts[1]})

        for name in names:
            # Name + state (most common search)
            for addr in self.addresses:
                if addr.state:
                    variants.append({**name, "state": addr.state, "city": addr.city})
            # Name-only fallback
            if not self.addresses:
                variants.append(name)
        return variants or [{"first_name": self.first_name, "last_name": self.last_name}]


# ---------------------------------------------------------------------------
# Listing — a record found on a broker site
# ---------------------------------------------------------------------------


class Listing(BaseModel):
    """A single record found on a broker site."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    broker_id: str  # e.g. "whitepages"
    profile_id: str
    url: str  # direct link to the listing
    found_name: str = ""
    found_location: str = ""
    found_age: str = ""
    raw_data: dict = Field(default_factory=dict)
    discovered_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    confidence: float = 0.0  # 0-1, how sure this is the right person


# ---------------------------------------------------------------------------
# Removal request — state machine for tracking opt-out progress
# ---------------------------------------------------------------------------


class RemovalState(enum.StrEnum):
    """
    State machine:

        DISCOVERED ──► SUBMITTED ──► PENDING ──► CONFIRMED ──► MONITORING
                           │            │                          │
                           ▼            ▼                          ▼
                        FAILED       FAILED                    RE_LISTED
                           │            │                          │
                           ▼            ▼                          ▼
                        (retry)     (retry)                   SUBMITTED
    """

    DISCOVERED = "discovered"  # found on a broker site
    SUBMITTED = "submitted"  # opt-out request sent
    PENDING = "pending"  # waiting on broker response
    CONFIRMED = "confirmed"  # broker acknowledged removal
    MONITORING = "monitoring"  # periodic re-checks
    RE_LISTED = "re_listed"  # data reappeared
    FAILED = "failed"  # submission or verification failed
    SKIPPED = "skipped"  # user chose not to remove

    # Valid transitions
    @staticmethod
    def transitions() -> dict[RemovalState, list[RemovalState]]:
        S = RemovalState
        return {
            S.DISCOVERED: [S.SUBMITTED, S.SKIPPED, S.FAILED],
            S.SUBMITTED: [S.PENDING, S.FAILED, S.CONFIRMED],
            S.PENDING: [S.CONFIRMED, S.FAILED],
            S.CONFIRMED: [S.MONITORING],
            S.MONITORING: [S.RE_LISTED, S.CONFIRMED],
            S.RE_LISTED: [S.SUBMITTED, S.SKIPPED],
            S.FAILED: [S.SUBMITTED, S.SKIPPED],
            S.SKIPPED: [S.SUBMITTED, S.DISCOVERED],
        }


class StateTransition(BaseModel):
    from_state: str
    to_state: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    reason: str = ""


class RemovalRequest(BaseModel):
    """Tracks an opt-out request through its lifecycle."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    listing_id: str
    broker_id: str
    profile_id: str
    state: RemovalState = RemovalState.DISCOVERED
    history: list[StateTransition] = Field(default_factory=list)
    submitted_at: str | None = None
    confirmed_at: str | None = None
    last_checked_at: str | None = None
    next_check_at: str | None = None
    attempts: int = 0
    notes: str = ""

    def transition(self, new_state: RemovalState, reason: str = "") -> None:
        valid = RemovalState.transitions().get(self.state, [])
        if new_state not in valid:
            raise ValueError(
                f"Invalid transition: {self.state.value} → {new_state.value}. "
                f"Valid: {[s.value for s in valid]}"
            )
        self.history.append(
            StateTransition(
                from_state=self.state.value,
                to_state=new_state.value,
                reason=reason,
            )
        )
        self.state = new_state
        now = datetime.now().isoformat()
        if new_state == RemovalState.SUBMITTED:
            self.submitted_at = now
            self.attempts += 1
        elif new_state == RemovalState.CONFIRMED:
            self.confirmed_at = now

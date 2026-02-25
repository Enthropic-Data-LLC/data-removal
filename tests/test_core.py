"""Tests for core models, state machine, and database."""

import pytest

from dataremoval.core.database import Database
from dataremoval.core.models import (
    Address,
    Listing,
    Profile,
    RemovalRequest,
    RemovalState,
)

# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def test_profile_full_name():
    p = Profile(first_name="John", last_name="Doe", middle_name="Q")
    assert p.full_name == "John Q Doe"


def test_profile_search_variants():
    p = Profile(
        first_name="John",
        last_name="Doe",
        aliases=["Johnny Doe"],
        addresses=[Address(city="Portland", state="OR")],
    )
    variants = p.search_variants()
    assert len(variants) >= 2
    assert variants[0]["first_name"] == "John"
    assert variants[0]["state"] == "OR"


def test_profile_search_variants_no_address():
    p = Profile(first_name="Jane", last_name="Smith")
    variants = p.search_variants()
    assert len(variants) == 1
    assert variants[0] == {"first_name": "Jane", "last_name": "Smith"}


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_valid_transitions():
    req = RemovalRequest(listing_id="l1", broker_id="test", profile_id="p1")
    assert req.state == RemovalState.DISCOVERED

    req.transition(RemovalState.SUBMITTED, "opt-out sent")
    assert req.state == RemovalState.SUBMITTED
    assert req.attempts == 1
    assert req.submitted_at is not None

    req.transition(RemovalState.CONFIRMED, "email verified")
    assert req.state == RemovalState.CONFIRMED
    assert req.confirmed_at is not None
    assert len(req.history) == 2


def test_invalid_transition():
    req = RemovalRequest(listing_id="l1", broker_id="test", profile_id="p1")
    with pytest.raises(ValueError, match="Invalid transition"):
        req.transition(RemovalState.CONFIRMED)  # can't skip SUBMITTED


def test_re_list_cycle():
    req = RemovalRequest(listing_id="l1", broker_id="test", profile_id="p1")
    req.transition(RemovalState.SUBMITTED)
    req.transition(RemovalState.CONFIRMED)
    req.transition(RemovalState.MONITORING)
    req.transition(RemovalState.RE_LISTED, "data reappeared")
    req.transition(RemovalState.SUBMITTED, "re-submitted")
    assert req.attempts == 2
    assert len(req.history) == 5


def test_failure_retry():
    req = RemovalRequest(listing_id="l1", broker_id="test", profile_id="p1")
    req.transition(RemovalState.SUBMITTED)
    req.transition(RemovalState.FAILED, "timeout")
    req.transition(RemovalState.SUBMITTED, "retry")
    assert req.attempts == 2


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def test_profile_crud(db):
    p = Profile(first_name="Test", last_name="User")
    db.save_profile(p)

    loaded = db.get_profile(p.id)
    assert loaded is not None
    assert loaded.full_name == "Test User"

    all_profiles = db.list_profiles()
    assert len(all_profiles) == 1

    db.delete_profile(p.id)
    assert db.get_profile(p.id) is None


def test_listing_storage(db):
    listing = Listing(
        broker_id="whitepages",
        profile_id="p1",
        url="https://whitepages.com/name/test-user",
        found_name="Test User",
    )
    db.save_listing(listing)

    results = db.get_listings(profile_id="p1")
    assert len(results) == 1
    assert results[0].broker_id == "whitepages"


def test_request_storage(db):
    req = RemovalRequest(listing_id="l1", broker_id="test", profile_id="p1")
    req.transition(RemovalState.SUBMITTED)
    db.save_request(req)

    loaded = db.get_request(req.id)
    assert loaded is not None
    assert loaded.state == RemovalState.SUBMITTED

    by_state = db.get_requests(state=RemovalState.SUBMITTED)
    assert len(by_state) == 1


def test_stats(db):
    p = Profile(first_name="Test", last_name="User")
    db.save_profile(p)

    listing = Listing(broker_id="test", profile_id=p.id, url="http://example.com")
    db.save_listing(listing)

    req = RemovalRequest(listing_id=listing.id, broker_id="test", profile_id=p.id)
    db.save_request(req)

    stats = db.stats(p.id)
    assert stats["listings"] == 1
    assert stats["requests_by_state"]["discovered"] == 1

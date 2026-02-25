"""Tests for shared broker utilities."""

from unittest.mock import AsyncMock, patch

import httpx  # noqa: F401 - used in mock side_effect
import pytest

from dataremoval.brokers._utils import (
    check_url_status,
    compute_confidence,
    deduplicate,
)
from dataremoval.core.models import Address, Listing, Profile

# ---------------------------------------------------------------------------
# compute_confidence
# ---------------------------------------------------------------------------


def test_confidence_exact_match():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        age=35,
        addresses=[Address(state="IL", city="Springfield")],
    )
    score = compute_confidence(profile, "Jane Smith", "Springfield, IL", "35")
    # Name (0.4) + state (0.2) + city (0.15) + age (0.15) = 0.9
    assert score >= 0.7


def test_confidence_name_only():
    profile = Profile(first_name="Jane", last_name="Smith")
    score = compute_confidence(profile, "Jane Smith", "Los Angeles, CA", "")
    assert 0.3 <= score <= 0.5


def test_confidence_partial_name():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL")],
    )
    score = compute_confidence(profile, "Jane M Smith", "Chicago, IL", "")
    # Partial name (0.3) + state (0.2) = 0.5
    assert 0.4 <= score <= 0.6


def test_confidence_age_text_format():
    """Handles 'Age 35' format (used by TruePeopleSearch)."""
    profile = Profile(first_name="Jane", last_name="Smith", age=35)
    score = compute_confidence(profile, "Jane Smith", "", "Age 35")
    # Name (0.4) + age (0.15) = 0.55
    assert score >= 0.5


def test_confidence_age_within_tolerance():
    profile = Profile(first_name="Jane", last_name="Smith", age=35)
    score = compute_confidence(profile, "Jane Smith", "", "36")
    # Name (0.4) + age (0.15, within 1 year) = 0.55
    assert score >= 0.5


def test_confidence_no_match():
    profile = Profile(first_name="Jane", last_name="Smith")
    score = compute_confidence(profile, "Bob Jones", "New York, NY", "")
    assert score == 0.0


def test_confidence_with_relatives():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        relatives=["Alice Smith", "Bob Smith"],
    )
    score = compute_confidence(profile, "Jane Smith", "", "", found_relatives=["Alice Smith"])
    # Name (0.4) + 1 relative (0.05) = 0.45
    assert score >= 0.4


def test_confidence_multiple_relatives():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        relatives=["Alice Smith", "Bob Smith"],
    )
    score = compute_confidence(
        profile, "Jane Smith", "", "", found_relatives=["Alice Smith", "Bob Smith"]
    )
    # Name (0.4) + 2 relatives (0.10) = 0.50
    assert score >= 0.5


def test_confidence_capped_at_1():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        age=35,
        addresses=[Address(state="IL", city="Springfield")],
        relatives=["Alice Smith", "Bob Smith"],
    )
    score = compute_confidence(
        profile, "Jane Smith", "Springfield, IL", "35", found_relatives=["Alice Smith", "Bob Smith"]
    )
    assert score == 1.0


# ---------------------------------------------------------------------------
# deduplicate
# ---------------------------------------------------------------------------


def test_deduplicate_removes_dupes():
    base = dict(broker_id="test", profile_id="abc")
    listings = [
        Listing(url="https://example.com/1", **base),
        Listing(url="https://example.com/2", **base),
        Listing(url="https://example.com/1", **base),
    ]
    result = deduplicate(listings)
    assert len(result) == 2
    assert [item.url for item in result] == [
        "https://example.com/1",
        "https://example.com/2",
    ]


def test_deduplicate_preserves_order():
    base = dict(broker_id="test", profile_id="abc")
    listings = [
        Listing(url="https://example.com/b", **base),
        Listing(url="https://example.com/a", **base),
        Listing(url="https://example.com/c", **base),
    ]
    result = deduplicate(listings)
    assert [item.url for item in result] == [
        "https://example.com/b",
        "https://example.com/a",
        "https://example.com/c",
    ]


def test_deduplicate_empty():
    assert deduplicate([]) == []


# ---------------------------------------------------------------------------
# check_url_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_url_status_removed():
    mock_resp = AsyncMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers._utils.httpx.AsyncClient", return_value=mock_client):
        result = await check_url_status("https://example.com/p/1")
    assert result == "removed"


@pytest.mark.asyncio
async def test_check_url_status_410():
    mock_resp = AsyncMock()
    mock_resp.status_code = 410

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers._utils.httpx.AsyncClient", return_value=mock_client):
        result = await check_url_status("https://example.com/p/1")
    assert result == "removed"


@pytest.mark.asyncio
async def test_check_url_status_still_listed():
    mock_resp = AsyncMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers._utils.httpx.AsyncClient", return_value=mock_client):
        result = await check_url_status("https://example.com/p/1")
    assert result == "still_listed"


@pytest.mark.asyncio
async def test_check_url_status_403_unknown():
    """403 treated as unknown by default (WAF block)."""
    mock_resp = AsyncMock()
    mock_resp.status_code = 403

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers._utils.httpx.AsyncClient", return_value=mock_client):
        result = await check_url_status("https://example.com/p/1")
    assert result == "unknown"


@pytest.mark.asyncio
async def test_check_url_status_403_still_listed():
    """403 treated as still_listed when treat_403_as_unknown=False."""
    mock_resp = AsyncMock()
    mock_resp.status_code = 403

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers._utils.httpx.AsyncClient", return_value=mock_client):
        result = await check_url_status("https://example.com/p/1", treat_403_as_unknown=False)
    assert result == "still_listed"


@pytest.mark.asyncio
async def test_check_url_status_exception():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection failed"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers._utils.httpx.AsyncClient", return_value=mock_client):
        result = await check_url_status("https://example.com/p/1")
    assert result == "unknown"

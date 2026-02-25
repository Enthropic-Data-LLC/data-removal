"""Tests for broker plugin system."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from dataremoval.brokers import registry
from dataremoval.brokers.spokeo import (
    SpokeoPlugin,
    _build_search_urls,
    _compute_confidence,
    _deduplicate,
    _normalize_name,
)
from dataremoval.core.models import Address, Listing, Profile

# ---------------------------------------------------------------------------
# Registry tests (existing)
# ---------------------------------------------------------------------------


def test_registry_discover():
    """Verify built-in plugins are auto-discovered."""
    registry.discover()
    assert len(registry) >= 3  # whitepages, spokeo, truepeoplesearch
    assert "whitepages" in registry.ids()
    assert "spokeo" in registry.ids()
    assert "truepeoplesearch" in registry.ids()


def test_registry_get():
    registry.discover()
    wp = registry.get("whitepages")
    assert wp is not None
    assert wp.info().name == "Whitepages"


def test_broker_info_complete():
    """Each plugin should have complete metadata."""
    registry.discover()
    for plugin in registry.all():
        info = plugin.info()
        assert info.id
        assert info.name
        assert info.url.startswith("http")
        assert info.opt_out_url.startswith("http")
        assert info.expected_days > 0
        assert info.recheck_days > 0


# ---------------------------------------------------------------------------
# Spokeo — info
# ---------------------------------------------------------------------------


def test_info_fields():
    plugin = SpokeoPlugin()
    info = plugin.info()
    assert info.id == "spokeo"
    assert info.name == "Spokeo"
    assert info.url == "https://www.spokeo.com"
    assert info.opt_out_url == "https://www.spokeo.com/optout"
    assert info.difficulty.value == "medium"
    assert info.expected_days == 3
    assert info.recheck_days == 90


# ---------------------------------------------------------------------------
# Spokeo — helper functions
# ---------------------------------------------------------------------------


def test_normalize_name():
    assert _normalize_name("Jane Doe") == "Jane-Doe"
    assert _normalize_name("Mary") == "Mary"
    assert _normalize_name("  John   Doe  ") == "John-Doe"


def test_search_builds_correct_urls():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL", city="Springfield")],
    )
    urls = _build_search_urls(profile)
    assert len(urls) == 1
    assert urls[0] == "https://www.spokeo.com/Jane-Smith/IL"


def test_search_builds_urls_no_state():
    profile = Profile(first_name="Jane", last_name="Smith")
    urls = _build_search_urls(profile)
    assert len(urls) == 1
    assert urls[0] == "https://www.spokeo.com/Jane-Smith"


def test_search_builds_urls_with_aliases():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        aliases=["Jenny Smith"],
        addresses=[Address(state="IL")],
    )
    urls = _build_search_urls(profile)
    assert len(urls) == 2
    assert "https://www.spokeo.com/Jane-Smith/IL" in urls
    assert "https://www.spokeo.com/Jenny-Smith/IL" in urls


def test_search_deduplicates_by_url():
    base = dict(broker_id="spokeo", profile_id="abc")
    listings = [
        Listing(url="https://www.spokeo.com/p/1", **base),
        Listing(url="https://www.spokeo.com/p/2", **base),
        Listing(url="https://www.spokeo.com/p/1", **base),
    ]
    result = _deduplicate(listings)
    assert len(result) == 2
    assert [item.url for item in result] == [
        "https://www.spokeo.com/p/1",
        "https://www.spokeo.com/p/2",
    ]


# ---------------------------------------------------------------------------
# Spokeo — confidence scoring
# ---------------------------------------------------------------------------


def test_confidence_exact_match():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        age=35,
        addresses=[Address(state="IL", city="Springfield")],
    )
    score = _compute_confidence(profile, "Jane Smith", "Springfield, IL", "35")
    assert score >= 0.7


def test_confidence_name_only():
    profile = Profile(first_name="Jane", last_name="Smith")
    score = _compute_confidence(profile, "Jane Smith", "Los Angeles, CA", "")
    assert 0.3 <= score <= 0.5


def test_confidence_partial_name():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL")],
    )
    score = _compute_confidence(profile, "Jane M Smith", "Chicago, IL", "")
    # Partial name match (0.3) + state (0.2) = 0.5
    assert 0.4 <= score <= 0.6


def test_confidence_with_relatives():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        relatives=["Alice Smith", "Bob Smith"],
    )
    score = _compute_confidence(profile, "Jane Smith", "", "", found_relatives=["Alice Smith"])
    # Name (0.4) + 1 relative (0.05)
    assert score >= 0.4


# ---------------------------------------------------------------------------
# Spokeo — search (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_empty_without_playwright():
    plugin = SpokeoPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.spokeo.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


# ---------------------------------------------------------------------------
# Spokeo — opt-out (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opt_out_returns_false_without_playwright():
    plugin = SpokeoPlugin()
    listing = Listing(
        broker_id="spokeo",
        profile_id="abc",
        url="https://www.spokeo.com/p/1",
        raw_data={"opt_out_email": "test@example.com"},
    )
    with patch("dataremoval.brokers.spokeo.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


@pytest.mark.asyncio
async def test_opt_out_returns_false_without_email():
    plugin = SpokeoPlugin()
    listing = Listing(
        broker_id="spokeo",
        profile_id="abc",
        url="https://www.spokeo.com/p/1",
        raw_data={},
    )
    result = await plugin.submit_opt_out(listing)
    assert result is False


# ---------------------------------------------------------------------------
# Spokeo — check_status (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_status_removed():
    plugin = SpokeoPlugin()
    listing = Listing(broker_id="spokeo", profile_id="abc", url="https://www.spokeo.com/p/1")
    mock_resp = AsyncMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers.spokeo.httpx.AsyncClient", return_value=mock_client):
        result = await plugin.check_status(listing)
    assert result == "removed"


@pytest.mark.asyncio
async def test_check_status_still_listed():
    plugin = SpokeoPlugin()
    listing = Listing(broker_id="spokeo", profile_id="abc", url="https://www.spokeo.com/p/1")
    mock_resp = AsyncMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers.spokeo.httpx.AsyncClient", return_value=mock_client):
        result = await plugin.check_status(listing)
    assert result == "still_listed"


@pytest.mark.asyncio
async def test_check_status_exception():
    plugin = SpokeoPlugin()
    listing = Listing(broker_id="spokeo", profile_id="abc", url="https://www.spokeo.com/p/1")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection failed"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("dataremoval.brokers.spokeo.httpx.AsyncClient", return_value=mock_client):
        result = await plugin.check_status(listing)
    assert result == "unknown"

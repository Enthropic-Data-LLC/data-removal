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
from dataremoval.brokers.truepeoplesearch import (
    TruePeopleSearchPlugin,
)
from dataremoval.brokers.truepeoplesearch import (
    _build_search_url as tps_build_search_url,
)
from dataremoval.brokers.truepeoplesearch import (
    _build_search_urls as tps_build_search_urls,
)
from dataremoval.brokers.truepeoplesearch import (
    _compute_confidence as tps_compute_confidence,
)
from dataremoval.brokers.truepeoplesearch import (
    _deduplicate as tps_deduplicate,
)
from dataremoval.brokers.truepeoplesearch import (
    _is_profile_url as tps_is_profile_url,
)
from dataremoval.brokers.whitepages import (
    WhitepagesPlugin,
)
from dataremoval.brokers.whitepages import (
    _build_search_url as wp_build_search_url,
)
from dataremoval.brokers.whitepages import (
    _build_search_urls as wp_build_search_urls,
)
from dataremoval.brokers.whitepages import (
    _compute_confidence as wp_compute_confidence,
)
from dataremoval.brokers.whitepages import (
    _deduplicate as wp_deduplicate,
)
from dataremoval.brokers.whitepages import (
    _is_profile_url as wp_is_profile_url,
)
from dataremoval.brokers.whitepages import (
    _normalize_name as wp_normalize_name,
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


# ===========================================================================
# TruePeopleSearch tests
# ===========================================================================


# ---------------------------------------------------------------------------
# TruePeopleSearch — info
# ---------------------------------------------------------------------------


def test_truepeoplesearch_info_fields():
    plugin = TruePeopleSearchPlugin()
    info = plugin.info()
    assert info.id == "truepeoplesearch"
    assert info.name == "TruePeopleSearch"
    assert info.url == "https://www.truepeoplesearch.com"
    assert info.opt_out_url == "https://www.truepeoplesearch.com/removal"
    assert info.difficulty.value == "easy"
    assert info.expected_days == 1
    assert info.recheck_days == 90
    assert "Cloudflare" in info.notes


# ---------------------------------------------------------------------------
# TruePeopleSearch — helper functions
# ---------------------------------------------------------------------------


def test_tps_build_search_url_with_location():
    url = tps_build_search_url("Jane", "Smith", "Springfield", "IL")
    assert url == (
        "https://www.truepeoplesearch.com/results?name=Jane+Smith&citystatezip=Springfield+IL"
    )


def test_tps_build_search_url_name_only():
    url = tps_build_search_url("Jane", "Smith")
    assert url == "https://www.truepeoplesearch.com/results?name=Jane+Smith"


def test_tps_build_search_url_state_only():
    url = tps_build_search_url("Jane", "Smith", "", "IL")
    assert url == "https://www.truepeoplesearch.com/results?name=Jane+Smith&citystatezip=IL"


def test_tps_build_search_urls_from_profile():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL", city="Springfield")],
    )
    urls = tps_build_search_urls(profile)
    assert len(urls) == 1
    assert "name=Jane+Smith" in urls[0]
    assert "citystatezip=Springfield+IL" in urls[0]


def test_tps_build_search_urls_with_aliases():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        aliases=["Jenny Smith"],
        addresses=[Address(state="IL", city="Springfield")],
    )
    urls = tps_build_search_urls(profile)
    assert len(urls) == 2
    assert any("Jane+Smith" in u for u in urls)
    assert any("Jenny+Smith" in u for u in urls)


def test_tps_build_search_urls_no_address():
    profile = Profile(first_name="Jane", last_name="Smith")
    urls = tps_build_search_urls(profile)
    assert len(urls) == 1
    assert "citystatezip" not in urls[0]


def test_tps_is_profile_url_valid():
    assert tps_is_profile_url("https://www.truepeoplesearch.com/find/person/1a2b3c4d5e") is True
    assert tps_is_profile_url("/find/person/abc123def456") is True


def test_tps_is_profile_url_invalid():
    assert tps_is_profile_url("https://www.truepeoplesearch.com/results") is False
    assert tps_is_profile_url("https://www.truepeoplesearch.com/removal") is False
    assert tps_is_profile_url("https://www.google.com") is False


def test_tps_deduplicate():
    base = dict(broker_id="truepeoplesearch", profile_id="abc")
    listings = [
        Listing(url="https://www.truepeoplesearch.com/find/person/aaa", **base),
        Listing(url="https://www.truepeoplesearch.com/find/person/bbb", **base),
        Listing(url="https://www.truepeoplesearch.com/find/person/aaa", **base),
    ]
    result = tps_deduplicate(listings)
    assert len(result) == 2
    assert [item.url for item in result] == [
        "https://www.truepeoplesearch.com/find/person/aaa",
        "https://www.truepeoplesearch.com/find/person/bbb",
    ]


# ---------------------------------------------------------------------------
# TruePeopleSearch — confidence scoring
# ---------------------------------------------------------------------------


def test_tps_confidence_exact_match():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        age=35,
        addresses=[Address(state="IL", city="Springfield")],
    )
    score = tps_compute_confidence(profile, "Jane Smith", "Springfield, IL", "Age 35")
    assert score >= 0.7


def test_tps_confidence_name_only():
    profile = Profile(first_name="Jane", last_name="Smith")
    score = tps_compute_confidence(profile, "Jane Smith", "Los Angeles, CA", "")
    assert 0.3 <= score <= 0.5


def test_tps_confidence_partial_name():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL")],
    )
    score = tps_compute_confidence(profile, "Jane M Smith", "Chicago, IL", "")
    # Partial name (0.3) + state (0.2) = 0.5
    assert 0.4 <= score <= 0.6


def test_tps_confidence_age_text_format():
    """TruePeopleSearch shows age as 'Age 35', not just '35'."""
    profile = Profile(first_name="Jane", last_name="Smith", age=35)
    score = tps_compute_confidence(profile, "Jane Smith", "", "Age 35")
    # Name (0.4) + age (0.15) = 0.55
    assert score >= 0.5


def test_tps_confidence_with_relatives():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        relatives=["Alice Smith", "Bob Smith"],
    )
    score = tps_compute_confidence(profile, "Jane Smith", "", "", found_relatives=["Alice Smith"])
    # Name (0.4) + 1 relative (0.05) = 0.45
    assert score >= 0.4


# ---------------------------------------------------------------------------
# TruePeopleSearch — search (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tps_search_returns_empty_without_playwright():
    plugin = TruePeopleSearchPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.truepeoplesearch.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


# ---------------------------------------------------------------------------
# TruePeopleSearch — opt-out (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tps_opt_out_returns_false_without_playwright():
    plugin = TruePeopleSearchPlugin()
    listing = Listing(
        broker_id="truepeoplesearch",
        profile_id="abc",
        url="https://www.truepeoplesearch.com/find/person/aaa",
    )
    with patch("dataremoval.brokers.truepeoplesearch.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


# ---------------------------------------------------------------------------
# TruePeopleSearch — check_status (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tps_check_status_removed():
    plugin = TruePeopleSearchPlugin()
    listing = Listing(
        broker_id="truepeoplesearch",
        profile_id="abc",
        url="https://www.truepeoplesearch.com/find/person/aaa",
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dataremoval.brokers.truepeoplesearch.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await plugin.check_status(listing)
    assert result == "removed"


@pytest.mark.asyncio
async def test_tps_check_status_still_listed():
    plugin = TruePeopleSearchPlugin()
    listing = Listing(
        broker_id="truepeoplesearch",
        profile_id="abc",
        url="https://www.truepeoplesearch.com/find/person/aaa",
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dataremoval.brokers.truepeoplesearch.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await plugin.check_status(listing)
    assert result == "still_listed"


@pytest.mark.asyncio
async def test_tps_check_status_cloudflare_403():
    """Cloudflare 403 should return 'unknown', not 'still_listed'."""
    plugin = TruePeopleSearchPlugin()
    listing = Listing(
        broker_id="truepeoplesearch",
        profile_id="abc",
        url="https://www.truepeoplesearch.com/find/person/aaa",
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 403

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dataremoval.brokers.truepeoplesearch.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await plugin.check_status(listing)
    assert result == "unknown"


@pytest.mark.asyncio
async def test_tps_check_status_exception():
    plugin = TruePeopleSearchPlugin()
    listing = Listing(
        broker_id="truepeoplesearch",
        profile_id="abc",
        url="https://www.truepeoplesearch.com/find/person/aaa",
    )
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection failed"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dataremoval.brokers.truepeoplesearch.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await plugin.check_status(listing)
    assert result == "unknown"


# ===========================================================================
# Whitepages tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Whitepages — info
# ---------------------------------------------------------------------------


def test_whitepages_info_fields():
    plugin = WhitepagesPlugin()
    info = plugin.info()
    assert info.id == "whitepages"
    assert info.name == "Whitepages"
    assert info.url == "https://www.whitepages.com"
    assert info.opt_out_url == "https://www.whitepages.com/suppression-requests"
    assert info.difficulty.value == "easy"
    assert info.expected_days == 2
    assert info.recheck_days == 90
    assert "phone" in info.notes.lower()


# ---------------------------------------------------------------------------
# Whitepages — helper functions
# ---------------------------------------------------------------------------


def test_wp_normalize_name():
    assert wp_normalize_name("Jane Doe") == "Jane-Doe"
    assert wp_normalize_name("Mary") == "Mary"
    assert wp_normalize_name("  John   Doe  ") == "John-Doe"


def test_wp_build_search_url_with_location():
    url = wp_build_search_url("Jane", "Smith", "Springfield", "IL")
    assert url == "https://www.whitepages.com/name/Jane-Smith/Springfield-IL"


def test_wp_build_search_url_name_only():
    url = wp_build_search_url("Jane", "Smith")
    assert url == "https://www.whitepages.com/name/Jane-Smith"


def test_wp_build_search_urls_from_profile():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL", city="Springfield")],
    )
    urls = wp_build_search_urls(profile)
    assert len(urls) == 1
    assert urls[0] == "https://www.whitepages.com/name/Jane-Smith/Springfield-IL"


def test_wp_build_search_urls_with_aliases():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        aliases=["Jenny Smith"],
        addresses=[Address(state="IL", city="Springfield")],
    )
    urls = wp_build_search_urls(profile)
    assert len(urls) == 2
    assert any("Jane-Smith" in u for u in urls)
    assert any("Jenny-Smith" in u for u in urls)


def test_wp_build_search_urls_no_address():
    profile = Profile(first_name="Jane", last_name="Smith")
    urls = wp_build_search_urls(profile)
    assert len(urls) == 1
    assert urls[0] == "https://www.whitepages.com/name/Jane-Smith"


def test_wp_is_profile_url_valid():
    assert wp_is_profile_url("https://www.whitepages.com/name/Jane-Smith/Springfield-IL") is True
    assert wp_is_profile_url("/name/Jane-Smith/Springfield-IL") is True


def test_wp_is_profile_url_invalid():
    assert wp_is_profile_url("https://www.whitepages.com/suppression-requests") is False
    assert wp_is_profile_url("https://www.whitepages.com/name/Jane-Smith") is False
    assert wp_is_profile_url("https://www.google.com") is False


def test_wp_deduplicate():
    base = dict(broker_id="whitepages", profile_id="abc")
    listings = [
        Listing(url="https://www.whitepages.com/name/Jane-Smith/Springfield-IL", **base),
        Listing(url="https://www.whitepages.com/name/Jane-Smith/Chicago-IL", **base),
        Listing(url="https://www.whitepages.com/name/Jane-Smith/Springfield-IL", **base),
    ]
    result = wp_deduplicate(listings)
    assert len(result) == 2
    assert [item.url for item in result] == [
        "https://www.whitepages.com/name/Jane-Smith/Springfield-IL",
        "https://www.whitepages.com/name/Jane-Smith/Chicago-IL",
    ]


# ---------------------------------------------------------------------------
# Whitepages — confidence scoring
# ---------------------------------------------------------------------------


def test_wp_confidence_exact_match():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        age=35,
        addresses=[Address(state="IL", city="Springfield")],
    )
    score = wp_compute_confidence(profile, "Jane Smith", "Springfield, IL", "Age 35")
    assert score >= 0.7


def test_wp_confidence_name_only():
    profile = Profile(first_name="Jane", last_name="Smith")
    score = wp_compute_confidence(profile, "Jane Smith", "Los Angeles, CA", "")
    assert 0.3 <= score <= 0.5


def test_wp_confidence_partial_name():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL")],
    )
    score = wp_compute_confidence(profile, "Jane M Smith", "Chicago, IL", "")
    # Partial name (0.3) + state (0.2) = 0.5
    assert 0.4 <= score <= 0.6


def test_wp_confidence_with_relatives():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        relatives=["Alice Smith", "Bob Smith"],
    )
    score = wp_compute_confidence(profile, "Jane Smith", "", "", found_relatives=["Alice Smith"])
    # Name (0.4) + 1 relative (0.05) = 0.45
    assert score >= 0.4


# ---------------------------------------------------------------------------
# Whitepages — search (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wp_search_returns_empty_without_playwright():
    plugin = WhitepagesPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.whitepages.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


# ---------------------------------------------------------------------------
# Whitepages — opt-out (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wp_opt_out_returns_false_without_playwright():
    plugin = WhitepagesPlugin()
    listing = Listing(
        broker_id="whitepages",
        profile_id="abc",
        url="https://www.whitepages.com/name/Jane-Smith/Springfield-IL",
    )
    with patch("dataremoval.brokers.whitepages.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


# ---------------------------------------------------------------------------
# Whitepages — check_status (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wp_check_status_removed():
    plugin = WhitepagesPlugin()
    listing = Listing(
        broker_id="whitepages",
        profile_id="abc",
        url="https://www.whitepages.com/name/Jane-Smith/Springfield-IL",
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dataremoval.brokers.whitepages.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await plugin.check_status(listing)
    assert result == "removed"


@pytest.mark.asyncio
async def test_wp_check_status_still_listed():
    plugin = WhitepagesPlugin()
    listing = Listing(
        broker_id="whitepages",
        profile_id="abc",
        url="https://www.whitepages.com/name/Jane-Smith/Springfield-IL",
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dataremoval.brokers.whitepages.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await plugin.check_status(listing)
    assert result == "still_listed"


@pytest.mark.asyncio
async def test_wp_check_status_cloudflare_403():
    """Cloudflare 403 should return 'unknown', not 'still_listed'."""
    plugin = WhitepagesPlugin()
    listing = Listing(
        broker_id="whitepages",
        profile_id="abc",
        url="https://www.whitepages.com/name/Jane-Smith/Springfield-IL",
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 403

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dataremoval.brokers.whitepages.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await plugin.check_status(listing)
    assert result == "unknown"


@pytest.mark.asyncio
async def test_wp_check_status_exception():
    plugin = WhitepagesPlugin()
    listing = Listing(
        broker_id="whitepages",
        profile_id="abc",
        url="https://www.whitepages.com/name/Jane-Smith/Springfield-IL",
    )
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection failed"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "dataremoval.brokers.whitepages.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await plugin.check_status(listing)
    assert result == "unknown"

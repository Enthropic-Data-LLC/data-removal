"""Tests for broker plugin system."""

from unittest.mock import patch

import pytest

from dataremoval.brokers import registry
from dataremoval.brokers.beenverified import BeenVerifiedPlugin
from dataremoval.brokers.beenverified import _build_search_url as bv_build_search_url
from dataremoval.brokers.beenverified import _build_search_urls as bv_build_search_urls
from dataremoval.brokers.beenverified import _is_profile_url as bv_is_profile_url
from dataremoval.brokers.fastpeoplesearch import FastPeopleSearchPlugin
from dataremoval.brokers.fastpeoplesearch import _build_search_url as fps_build_search_url
from dataremoval.brokers.fastpeoplesearch import _build_search_urls as fps_build_search_urls
from dataremoval.brokers.fastpeoplesearch import _is_profile_url as fps_is_profile_url
from dataremoval.brokers.intelius import InteliusPlugin
from dataremoval.brokers.intelius import _build_search_url as int_build_search_url
from dataremoval.brokers.intelius import _build_search_urls as int_build_search_urls
from dataremoval.brokers.intelius import _is_profile_url as int_is_profile_url
from dataremoval.brokers.peoplefinder import PeopleFinderPlugin
from dataremoval.brokers.peoplefinder import _build_search_url as pf_build_search_url
from dataremoval.brokers.peoplefinder import _build_search_urls as pf_build_search_urls
from dataremoval.brokers.peoplefinder import _is_profile_url as pf_is_profile_url
from dataremoval.brokers.radaris import RadarisPlugin
from dataremoval.brokers.radaris import _build_search_url as rad_build_search_url
from dataremoval.brokers.radaris import _build_search_urls as rad_build_search_urls
from dataremoval.brokers.radaris import _is_profile_url as rad_is_profile_url
from dataremoval.brokers.spokeo import (
    SpokeoPlugin,
    _build_search_urls,
    _is_profile_url,
    _normalize_name,
)
from dataremoval.brokers.thatsthem import ThatsThemPlugin
from dataremoval.brokers.thatsthem import _build_search_url as tt_build_search_url
from dataremoval.brokers.thatsthem import _build_search_urls as tt_build_search_urls
from dataremoval.brokers.thatsthem import _is_profile_url as tt_is_profile_url
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
    _is_profile_url as tps_is_profile_url,
)
from dataremoval.brokers.usphonebook import USPhonebookPlugin
from dataremoval.brokers.usphonebook import _build_search_url as usph_build_search_url
from dataremoval.brokers.usphonebook import _build_search_urls as usph_build_search_urls
from dataremoval.brokers.usphonebook import _is_profile_url as usph_is_profile_url
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
    _is_profile_url as wp_is_profile_url,
)
from dataremoval.brokers.whitepages import (
    _normalize_name as wp_normalize_name,
)
from dataremoval.core.models import Address, Listing, Profile

# ---------------------------------------------------------------------------
# Registry tests
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


# ===========================================================================
# Spokeo tests
# ===========================================================================


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


def test_spokeo_is_profile_url_valid():
    assert _is_profile_url("https://www.spokeo.com/Jane-Smith/IL/p12345678901") is True
    assert _is_profile_url("/Jane-Smith/IL/p12345678901") is True


def test_spokeo_is_profile_url_invalid():
    assert _is_profile_url("https://www.spokeo.com/people/search") is False
    assert _is_profile_url("https://www.spokeo.com/privacy") is False


@pytest.mark.asyncio
async def test_search_returns_empty_without_playwright():
    plugin = SpokeoPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.spokeo.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


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


# ===========================================================================
# TruePeopleSearch tests
# ===========================================================================


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
    assert "DataDome" in info.notes


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


@pytest.mark.asyncio
async def test_tps_search_returns_empty_without_playwright():
    plugin = TruePeopleSearchPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.truepeoplesearch.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


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


# ===========================================================================
# Whitepages tests
# ===========================================================================


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
    assert (
        wp_is_profile_url("https://www.whitepages.com/name/Jane-Smith/Springfield-IL/PX3vB7kO0L3")
        is True
    )
    assert wp_is_profile_url("/name/Jane-Smith/Springfield-IL/PAyLqRDVZz3") is True


def test_wp_is_profile_url_invalid():
    assert wp_is_profile_url("https://www.whitepages.com/suppression-requests") is False
    assert wp_is_profile_url("https://www.whitepages.com/name/Jane-Smith") is False
    # Search page URL (no person ID) should not match
    assert wp_is_profile_url("https://www.whitepages.com/name/Jane-Smith/Springfield-IL") is False
    assert wp_is_profile_url("https://www.google.com") is False


@pytest.mark.asyncio
async def test_wp_search_returns_empty_without_playwright():
    plugin = WhitepagesPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.whitepages.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


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


# ===========================================================================
# FastPeopleSearch tests
# ===========================================================================


def test_fps_info_fields():
    plugin = FastPeopleSearchPlugin()
    info = plugin.info()
    assert info.id == "fastpeoplesearch"
    assert info.name == "FastPeopleSearch"
    assert info.url == "https://www.fastpeoplesearch.com"
    assert info.opt_out_url == "https://www.fastpeoplesearch.com/removal"
    assert info.difficulty.value == "easy"
    assert info.expected_days == 3


def test_fps_build_search_url_with_location():
    url = fps_build_search_url("Jane", "Smith", "Springfield", "IL")
    assert url == "https://www.fastpeoplesearch.com/name/Jane-Smith_Springfield-IL"


def test_fps_build_search_url_name_only():
    url = fps_build_search_url("Jane", "Smith")
    assert url == "https://www.fastpeoplesearch.com/name/Jane-Smith"


def test_fps_build_search_urls_from_profile():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL", city="Springfield")],
    )
    urls = fps_build_search_urls(profile)
    assert len(urls) == 1
    assert "Jane-Smith_Springfield-IL" in urls[0]


def test_fps_is_profile_url_valid():
    assert fps_is_profile_url("https://www.fastpeoplesearch.com/david-brown_id_G-123") is True
    assert fps_is_profile_url("/Jane-Smith_Springfield-IL") is True


def test_fps_is_profile_url_invalid():
    assert fps_is_profile_url("https://www.fastpeoplesearch.com/removal") is False
    assert fps_is_profile_url("https://www.google.com") is False


@pytest.mark.asyncio
async def test_fps_search_returns_empty_without_playwright():
    plugin = FastPeopleSearchPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.fastpeoplesearch.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


@pytest.mark.asyncio
async def test_fps_opt_out_returns_false_without_playwright():
    plugin = FastPeopleSearchPlugin()
    listing = Listing(
        broker_id="fastpeoplesearch",
        profile_id="abc",
        url="https://www.fastpeoplesearch.com/name/Jane-Smith",
    )
    with patch("dataremoval.brokers.fastpeoplesearch.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


# ===========================================================================
# ThatsThem tests
# ===========================================================================


def test_tt_info_fields():
    plugin = ThatsThemPlugin()
    info = plugin.info()
    assert info.id == "thatsthem"
    assert info.name == "ThatsThem"
    assert info.url == "https://thatsthem.com"
    assert info.opt_out_url == "https://thatsthem.com/optout"
    assert info.difficulty.value == "easy"
    assert info.expected_days == 14


def test_tt_build_search_url_with_location():
    url = tt_build_search_url("Jane", "Smith", "Springfield", "IL")
    assert url == "https://thatsthem.com/name/Jane-Smith/Springfield-IL"


def test_tt_build_search_url_name_only():
    url = tt_build_search_url("Jane", "Smith")
    assert url == "https://thatsthem.com/name/Jane-Smith"


def test_tt_build_search_urls_from_profile():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL", city="Springfield")],
    )
    urls = tt_build_search_urls(profile)
    assert len(urls) == 1
    assert "Jane-Smith/Springfield-IL" in urls[0]


def test_tt_is_profile_url_valid():
    assert tt_is_profile_url("https://thatsthem.com/name/Jane-Smith/") is True
    assert tt_is_profile_url("/name/Jane-Smith/Springfield-IL/") is True


def test_tt_is_profile_url_invalid():
    assert tt_is_profile_url("https://thatsthem.com/optout") is False
    assert tt_is_profile_url("https://www.google.com") is False


@pytest.mark.asyncio
async def test_tt_search_returns_empty_without_playwright():
    plugin = ThatsThemPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.thatsthem.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


@pytest.mark.asyncio
async def test_tt_opt_out_returns_false_without_playwright():
    plugin = ThatsThemPlugin()
    listing = Listing(
        broker_id="thatsthem",
        profile_id="abc",
        url="https://thatsthem.com/name/Jane-Smith/",
    )
    with patch("dataremoval.brokers.thatsthem.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


# ===========================================================================
# USPhonebook tests
# ===========================================================================


def test_usph_info_fields():
    plugin = USPhonebookPlugin()
    info = plugin.info()
    assert info.id == "usphonebook"
    assert info.name == "USPhonebook"
    assert info.url == "https://www.usphonebook.com"
    assert info.opt_out_url == "https://www.usphonebook.com/removal"
    assert info.difficulty.value == "easy"
    assert info.expected_days == 3


def test_usph_build_search_url_with_state():
    url = usph_build_search_url("Jane", "Smith", "Springfield", "IL")
    assert url == "https://www.usphonebook.com/jane-smith/illinois/springfield"


def test_usph_build_search_url_name_only():
    url = usph_build_search_url("Jane", "Smith")
    assert url == "https://www.usphonebook.com/jane-smith"


def test_usph_build_search_urls_from_profile():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL")],
    )
    urls = usph_build_search_urls(profile)
    assert len(urls) == 1
    assert "jane-smith/illinois" in urls[0]


def test_usph_is_profile_url_valid():
    assert usph_is_profile_url("https://www.usphonebook.com/david-brown/UEjM4kDMwQDO3IzR") is True
    assert usph_is_profile_url("/jane-smith/UcjMxADOwQDN4QTM5IjR") is True


def test_usph_is_profile_url_invalid():
    assert usph_is_profile_url("https://www.usphonebook.com/opt-out") is False
    assert usph_is_profile_url("https://www.google.com") is False


@pytest.mark.asyncio
async def test_usph_search_returns_empty_without_playwright():
    plugin = USPhonebookPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.usphonebook.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


@pytest.mark.asyncio
async def test_usph_opt_out_returns_false_without_playwright():
    plugin = USPhonebookPlugin()
    listing = Listing(
        broker_id="usphonebook",
        profile_id="abc",
        url="https://www.usphonebook.com/Jane-Smith/Illinois/abc123",
    )
    with patch("dataremoval.brokers.usphonebook.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


# ===========================================================================
# BeenVerified tests
# ===========================================================================


def test_bv_info_fields():
    plugin = BeenVerifiedPlugin()
    info = plugin.info()
    assert info.id == "beenverified"
    assert info.name == "BeenVerified"
    assert info.url == "https://www.beenverified.com"
    assert info.opt_out_url == "https://www.beenverified.com/svc/optout/search/optouts"
    assert info.difficulty.value == "easy"
    assert info.expected_days == 2


def test_bv_build_search_url_with_state():
    url = bv_build_search_url("Jane", "Smith", "Illinois")
    assert url == "https://www.beenverified.com/people/Jane-Smith/Illinois/"


def test_bv_build_search_url_name_only():
    url = bv_build_search_url("Jane", "Smith")
    assert url == "https://www.beenverified.com/people/Jane-Smith/"


def test_bv_build_search_urls_from_profile():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL")],
    )
    urls = bv_build_search_urls(profile)
    assert len(urls) == 1
    assert "Jane-Smith/IL/" in urls[0]


def test_bv_is_profile_url_valid():
    assert (
        bv_is_profile_url("https://www.beenverified.com/people/Jane-Smith/Illinois/Pabc123") is True
    )
    assert bv_is_profile_url("/people/Jane-Smith/IL/Pxyz789") is True


def test_bv_is_profile_url_invalid():
    assert bv_is_profile_url("https://www.beenverified.com/people/Jane-Smith/") is False
    assert bv_is_profile_url("https://www.google.com") is False


@pytest.mark.asyncio
async def test_bv_search_returns_empty_without_playwright():
    plugin = BeenVerifiedPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.beenverified.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


@pytest.mark.asyncio
async def test_bv_opt_out_returns_false_without_playwright():
    plugin = BeenVerifiedPlugin()
    listing = Listing(
        broker_id="beenverified",
        profile_id="abc",
        url="https://www.beenverified.com/people/Jane-Smith/Illinois/Pabc123",
    )
    with patch("dataremoval.brokers.beenverified.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


# ===========================================================================
# Radaris tests
# ===========================================================================


def test_rad_info_fields():
    plugin = RadarisPlugin()
    info = plugin.info()
    assert info.id == "radaris"
    assert info.name == "Radaris"
    assert info.url == "https://radaris.com"
    assert info.opt_out_url == "https://radaris.com/control/privacy"
    assert info.difficulty.value == "easy"
    assert info.expected_days == 1


def test_rad_build_search_url():
    url = rad_build_search_url("Jane", "Smith")
    assert url == "https://radaris.com/p/Jane-Smith/"


def test_rad_build_search_urls_deduplicates():
    """Radaris doesn't use location in URL, so aliases with same name deduplicate."""
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL"), Address(state="CA")],
    )
    urls = rad_build_search_urls(profile)
    # Both addresses produce same URL since Radaris doesn't use location
    assert len(urls) == 1
    assert urls[0] == "https://radaris.com/p/Jane-Smith/"


def test_rad_build_search_urls_with_aliases():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        aliases=["Jenny Smith"],
        addresses=[Address(state="IL")],
    )
    urls = rad_build_search_urls(profile)
    assert len(urls) == 2
    assert "https://radaris.com/p/Jane-Smith/" in urls
    assert "https://radaris.com/p/Jenny-Smith/" in urls


def test_rad_is_profile_url_valid():
    assert rad_is_profile_url("https://radaris.com/p/Jane-Smith/") is True
    assert rad_is_profile_url("/p/Jane-Smith/") is True


def test_rad_is_profile_url_invalid():
    assert rad_is_profile_url("https://radaris.com/control/privacy") is False
    assert rad_is_profile_url("https://www.google.com") is False


@pytest.mark.asyncio
async def test_rad_search_returns_empty_without_playwright():
    plugin = RadarisPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.radaris.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


@pytest.mark.asyncio
async def test_rad_opt_out_returns_false_without_playwright():
    plugin = RadarisPlugin()
    listing = Listing(
        broker_id="radaris",
        profile_id="abc",
        url="https://radaris.com/p/Jane-Smith/",
    )
    with patch("dataremoval.brokers.radaris.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


# ===========================================================================
# PeopleFinder tests
# ===========================================================================


def test_pf_info_fields():
    plugin = PeopleFinderPlugin()
    info = plugin.info()
    assert info.id == "peoplefinder"
    assert info.name == "PeopleFinder"
    assert info.url == "https://www.peoplefinder.com"
    assert info.opt_out_url == "https://www.peoplefinder.com/opt-out"
    assert info.difficulty.value == "medium"
    assert info.expected_days == 9


def test_pf_build_search_url_with_location():
    url = pf_build_search_url("Jane", "Smith", "Springfield", "IL")
    assert url == "https://www.peoplefinder.com/people/Jane-Smith/Springfield-IL"


def test_pf_build_search_url_name_only():
    url = pf_build_search_url("Jane", "Smith")
    assert url == "https://www.peoplefinder.com/people/Jane-Smith"


def test_pf_build_search_urls_from_profile():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL", city="Springfield")],
    )
    urls = pf_build_search_urls(profile)
    assert len(urls) == 1
    assert "Jane-Smith/Springfield-IL" in urls[0]


def test_pf_is_profile_url_valid():
    assert (
        pf_is_profile_url("https://www.peoplefinder.com/people/Jane-Smith/Illinois/abc123") is True
    )
    assert pf_is_profile_url("/people/Jane-Smith/IL/detail") is True


def test_pf_is_profile_url_invalid():
    assert pf_is_profile_url("https://www.peoplefinder.com/opt-out") is False
    assert pf_is_profile_url("https://www.google.com") is False


@pytest.mark.asyncio
async def test_pf_search_returns_empty_without_playwright():
    plugin = PeopleFinderPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.peoplefinder.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


@pytest.mark.asyncio
async def test_pf_opt_out_returns_false_without_playwright():
    plugin = PeopleFinderPlugin()
    listing = Listing(
        broker_id="peoplefinder",
        profile_id="abc",
        url="https://www.peoplefinder.com/people/Jane-Smith/Illinois/abc123",
    )
    with patch("dataremoval.brokers.peoplefinder.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False


# ===========================================================================
# Intelius tests
# ===========================================================================


def test_int_info_fields():
    plugin = InteliusPlugin()
    info = plugin.info()
    assert info.id == "intelius"
    assert info.name == "Intelius"
    assert info.url == "https://www.intelius.com"
    assert info.opt_out_url == "https://www.intelius.com/opt-out"
    assert info.difficulty.value == "medium"
    assert info.expected_days == 3
    assert info.mail_address == "PO Box 24025, Seattle, WA 98124"
    assert "PeopleConnect" in info.notes


def test_int_build_search_url_with_state():
    url = int_build_search_url("Jane", "Smith", "IL")
    assert url == "https://www.intelius.com/people-search/Jane-Smith/IL/"


def test_int_build_search_url_name_only():
    url = int_build_search_url("Jane", "Smith")
    assert url == "https://www.intelius.com/people-search/Jane-Smith/"


def test_int_build_search_urls_from_profile():
    profile = Profile(
        first_name="Jane",
        last_name="Smith",
        addresses=[Address(state="IL")],
    )
    urls = int_build_search_urls(profile)
    assert len(urls) == 1
    assert "Jane-Smith/IL/" in urls[0]


def test_int_is_profile_url_valid():
    assert int_is_profile_url("https://www.intelius.com/people-search/Jane-Smith/Illinois/") is True
    assert int_is_profile_url("/people-search/Jane-Smith/IL/") is True


def test_int_is_profile_url_invalid():
    assert int_is_profile_url("https://www.intelius.com/opt-out") is False
    assert int_is_profile_url("https://www.google.com") is False


@pytest.mark.asyncio
async def test_int_search_returns_empty_without_playwright():
    plugin = InteliusPlugin()
    profile = Profile(first_name="Jane", last_name="Smith")
    with patch("dataremoval.brokers.intelius.HAS_PLAYWRIGHT", False):
        result = await plugin.search(profile)
    assert result == []


@pytest.mark.asyncio
async def test_int_opt_out_returns_false_without_playwright():
    plugin = InteliusPlugin()
    listing = Listing(
        broker_id="intelius",
        profile_id="abc",
        url="https://www.intelius.com/people-search/Jane-Smith/Illinois/",
    )
    with patch("dataremoval.brokers.intelius.HAS_PLAYWRIGHT", False):
        result = await plugin.submit_opt_out(listing)
    assert result is False

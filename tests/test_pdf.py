"""Tests for PDF opt-out letter generation."""

from pathlib import Path

import pytest

from dataremoval.core.models import Address, Listing, Profile
from dataremoval.pdf import generate_opt_out_letter


@pytest.fixture()
def profile():
    return Profile(
        first_name="Jane",
        last_name="Doe",
        date_of_birth="1990-05-15",
        addresses=[Address(street="123 Main St", city="Springfield", state="IL", zip_code="62701")],
        email_addresses=["jane@example.com"],
    )


@pytest.fixture()
def listing():
    return Listing(
        broker_id="intelius",
        profile_id="abc123",
        url="https://www.intelius.com/people-search/Jane-Doe/IL/",
        found_name="Jane Doe",
        found_location="Springfield, IL",
    )


class TestGenerateOptOutLetter:
    def test_creates_pdf_file(self, profile, listing, tmp_path):
        out = tmp_path / "letter.pdf"
        result = generate_opt_out_letter(
            profile, listing, "Intelius", "PO Box 24025, Seattle, WA 98124", out
        )
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 0

    def test_returns_path_object(self, profile, listing, tmp_path):
        out = tmp_path / "letter.pdf"
        result = generate_opt_out_letter(
            profile, listing, "Intelius", "PO Box 24025, Seattle, WA 98124", out
        )
        assert isinstance(result, Path)

    def test_pdf_starts_with_header(self, profile, listing, tmp_path):
        out = tmp_path / "letter.pdf"
        generate_opt_out_letter(
            profile, listing, "Intelius", "PO Box 24025, Seattle, WA 98124", out
        )
        content = out.read_bytes()
        assert content[:5] == b"%PDF-"

    def test_accepts_string_path(self, profile, listing, tmp_path):
        out = str(tmp_path / "letter.pdf")
        result = generate_opt_out_letter(
            profile, listing, "Intelius", "PO Box 24025, Seattle, WA 98124", out
        )
        assert Path(out).exists()
        assert isinstance(result, Path)

    def test_profile_without_address(self, listing, tmp_path):
        profile = Profile(first_name="John", last_name="Smith")
        out = tmp_path / "letter.pdf"
        generate_opt_out_letter(
            profile, listing, "Intelius", "PO Box 24025, Seattle, WA 98124", out
        )
        assert out.exists()
        assert out.stat().st_size > 0

    def test_listing_without_optional_fields(self, profile, tmp_path):
        listing = Listing(
            broker_id="intelius",
            profile_id="abc123",
            url="https://www.intelius.com/people-search/Jane-Doe/IL/",
        )
        out = tmp_path / "letter.pdf"
        generate_opt_out_letter(
            profile, listing, "Intelius", "PO Box 24025, Seattle, WA 98124", out
        )
        assert out.exists()

"""PDF generation for mail-in opt-out letters.

Generates a formal US business letter citing privacy laws (CCPA, state
data protection statutes) requesting removal of personal information
from data broker sites.

Requires the ``fpdf2`` optional dependency::

    pip install data-removal[pdf]
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from dataremoval.core.models import Listing, Profile

log = logging.getLogger(__name__)

try:
    from fpdf import FPDF

    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False


def generate_opt_out_letter(
    profile: Profile,
    listing: Listing,
    broker_name: str,
    broker_address: str,
    output_path: str | Path,
) -> Path:
    """Generate a PDF opt-out letter.

    Args:
        profile: The person requesting removal.
        listing: The specific listing to be removed.
        broker_name: Display name of the broker company.
        broker_address: Mailing address for the broker.
        output_path: Where to save the PDF file.

    Returns:
        The Path to the generated PDF.

    Raises:
        RuntimeError: If fpdf2 is not installed.
    """
    if not HAS_FPDF:
        raise RuntimeError(
            "fpdf2 is required for PDF generation. Install with: pip install data-removal[pdf]"
        )

    output_path = Path(output_path)
    today = datetime.now().strftime("%B %d, %Y")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=25)

    # --- Header: Sender info ---
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 6, profile.full_name, new_x="LMARGIN", new_y="NEXT")
    if profile.addresses:
        addr = profile.addresses[0]
        if addr.street:
            pdf.cell(0, 6, addr.street, new_x="LMARGIN", new_y="NEXT")
        city_state = ", ".join(p for p in [addr.city, addr.state, addr.zip_code] if p)
        if city_state:
            pdf.cell(0, 6, city_state, new_x="LMARGIN", new_y="NEXT")
    if profile.email_addresses:
        pdf.cell(0, 6, profile.email_addresses[0], new_x="LMARGIN", new_y="NEXT")

    pdf.ln(8)

    # --- Date ---
    pdf.cell(0, 6, today, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # --- Recipient ---
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, broker_name, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    for line in broker_address.split(","):
        pdf.cell(0, 6, line.strip(), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # --- Subject ---
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Re: Request to Remove Personal Information", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # --- Body ---
    pdf.set_font("Helvetica", size=11)

    body_paragraphs = [
        "Dear Privacy Department,",
        (
            "I am writing to formally request the removal of my personal information "
            "from your database and website. I have identified the following listing "
            "that contains my data:"
        ),
        f"  Listing URL: {listing.url}",
        f"  Name found: {listing.found_name}" if listing.found_name else None,
        f"  Location: {listing.found_location}" if listing.found_location else None,
        (
            "Pursuant to applicable privacy laws, including but not limited to the "
            "California Consumer Privacy Act (CCPA/CPRA), the Virginia Consumer Data "
            "Protection Act (VCDPA), the Colorado Privacy Act, and other state data "
            "protection statutes, I hereby exercise my right to request the deletion "
            "of my personal information from your systems."
        ),
        (
            "Specifically, I request that you: (1) Remove the above-listed record and "
            "any associated personal information from your website and databases; "
            "(2) Cease selling, sharing, or otherwise disclosing my personal information "
            "to third parties; (3) Direct any service providers or contractors who have "
            "received my data to delete it as well."
        ),
        (
            "I request confirmation of this deletion within 45 days as required by law. "
            "If you require additional information to verify my identity, please contact "
            "me at the address above."
        ),
        "Thank you for your prompt attention to this matter.",
    ]

    for para in body_paragraphs:
        if para is None:
            continue
        pdf.ln(4)
        pdf.multi_cell(0, 6, para)

    # --- Signature block ---
    pdf.ln(12)
    pdf.cell(0, 6, "Sincerely,", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(16)
    pdf.cell(0, 6, profile.full_name, new_x="LMARGIN", new_y="NEXT")

    if profile.date_of_birth:
        pdf.set_font("Helvetica", size=9)
        pdf.ln(4)
        pdf.cell(0, 5, f"Date of Birth: {profile.date_of_birth}", new_x="LMARGIN", new_y="NEXT")

    pdf.output(str(output_path))
    log.info("Generated opt-out letter: %s", output_path)
    return output_path

# dr -- Data Removal CLI

Remove your personal data from people-search and data broker sites.

`dr` scans 10 major data brokers, finds your listings, submits opt-out requests,
and monitors for re-listings -- all from the command line. Your data never leaves
your machine (except when talking to broker sites).

Runs on **Linux**, **macOS**, and **Windows**. Requires **Python 3.11+**.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Installation](#installation)
- [Commands](#commands)
  - [Profile Management](#profile-management)
  - [Scanning](#scanning)
  - [Listing Management](#listing-management)
  - [Removal](#removal)
  - [Monitoring](#monitoring)
  - [Brokers](#brokers)
  - [Export & PDF Letters](#export--pdf-letters)
- [Supported Brokers](#supported-brokers)
- [Workflow Example](#workflow-example)
- [Confidence Scoring](#confidence-scoring)
- [State Machine](#state-machine)
- [Architecture](#architecture)
- [Adding a New Broker](#adding-a-new-broker)
- [Configuration](#configuration)
- [Testing](#testing)
- [Contributing](#contributing)

---

## Quick Start

```bash
# Install
git clone https://github.com/Enthropic-Data-LLC/data-removal.git
cd data-removal
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,browser]"
playwright install chromium

# Create a profile
dr profile add --first David --last Brown --city "Indian Trail" --state NC \
  --email you@example.com --alias "Dave Brown" --relative "Jane Brown"

# Scan broker sites
dr scan

# Review results, dismiss false positives
dr listings list
dr listings dismiss --below 0.3

# Submit opt-out requests
dr remove

# Check status later
dr status

# Re-check for re-listings
dr monitor
```

---

## Installation

### Cross-platform bootstrap (recommended)

```bash
python3 bootstrap.py
```

The bootstrap script auto-detects your OS, installs system dependencies if needed,
creates a virtual environment, installs the project, and runs tests.

```bash
python3 bootstrap.py --check      # Check prerequisites only
python3 bootstrap.py --browser    # Also install Playwright for form automation
```

### Manual setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # Core + test dependencies
pip install -e ".[dev,browser]"    # + Playwright for broker automation
pip install -e ".[dev,browser,pdf]" # + PDF letter generation
playwright install chromium        # Required for browser-based brokers
```

### Optional extras

| Extra | Packages | Purpose |
|-------|----------|---------|
| `browser` | `playwright>=1.40`, `playwright-stealth>=2.0` | Browser automation for search & opt-out |
| `pdf` | `fpdf2>=2.7` | Generate PDF opt-out letters for mailing |
| `dev` | `pytest>=8.0`, `pytest-asyncio>=0.23` | Testing |

### Dev commands

Linux / macOS:
```bash
make setup          # Create venv + install
make test           # Run tests
make lint           # Ruff + mypy
make fmt            # Auto-format
make browser-deps   # Install Playwright + chromium
make reset-db       # Delete local database
```

Windows:
```cmd
dev setup
dev test
dev lint
dev fmt
dev browser
dev reset-db
```

---

## Commands

### Profile Management

```bash
# Create a profile (interactive prompts for required fields)
dr profile add --first John --last Doe

# Create with full details for better matching
dr profile add \
  --first David --last Brown --middle Lee \
  --city "Indian Trail" --state NC \
  --email you@example.com \
  --phone "509-929-9570" \
  --alias "Dave Brown" --alias "David L Brown" \
  --relative "Jane Doe" --relative "John Doe Sr"

# List all profiles
dr profile list

# Show profile details
dr profile show abc123def456

# Edit an existing profile (only specified fields change)
dr profile edit abc123 --middle Lee --add-alias "Dave Brown"
dr profile edit abc123 --add-phone "555-123-4567" --add-relative "Jane Doe"

# Replace all values of a list field
dr profile edit abc123 --alias "Dave Brown" --alias "D. Brown"  # replaces all aliases

# Export to JSON, edit in your text editor, reimport
dr profile export abc123
# Edit profile-abc123.json in any editor...
dr profile import profile-abc123.json --merge

# Create a new profile from a JSON file
dr profile import profile.json

# Delete a profile
dr profile delete abc123
dr profile delete abc123 --force  # skip confirmation
```

**Profile JSON format** (exported by `dr profile export`):

```json
{
  "id": "7be82f330381",
  "first_name": "David",
  "last_name": "Brown",
  "middle_name": "Lee",
  "aliases": ["David L Brown", "Dave Brown"],
  "date_of_birth": "1966-05-21",
  "age": 59,
  "addresses": [
    {
      "street": "123 Main St",
      "city": "Indian Trail",
      "state": "NC",
      "zip_code": "28079",
      "current": true
    }
  ],
  "phone_numbers": ["509-929-9570"],
  "email_addresses": ["you@example.com"],
  "relatives": ["Laura Brown", "Jonathan Brown"]
}
```

### Scanning

```bash
# Scan all brokers
dr scan

# Scan a specific broker
dr scan --broker whitepages

# Scan for a specific profile (when you have multiple)
dr scan --profile abc123
```

Output shows a table of found listings with broker, name, location, and URL.
Listings are saved to the local database with confidence scores.

### Listing Management

After scanning, review and filter results:

```bash
# View all listings with confidence scores
dr listings list

# Filter by broker
dr listings list --broker spokeo

# Show dismissed listings too
dr listings list --dismissed

# Dismiss specific listings by ID prefix
dr listings dismiss abc123 def456

# Bulk dismiss all listings below 30% confidence
dr listings dismiss --below 0.3

# Dismiss low-confidence results from a specific broker
dr listings dismiss --below 0.5 --broker spokeo

# Undo a dismissal
dr listings restore abc123

# Restore all dismissed listings
dr listings restore --all
```

### Removal

```bash
# Submit opt-out requests for all discovered listings
dr remove

# Preview what would be submitted (no actual requests)
dr remove --dry-run

# Submit for a specific broker only
dr remove --broker whitepages
```

Some brokers require manual steps:
- **Whitepages**: Opens a visible browser for phone call verification
- **Spokeo**: Sends confirmation email you must click
- **FastPeopleSearch**: May require solving a CAPTCHA in a visible browser

### Monitoring

```bash
# Re-check all confirmed removals
dr monitor

# Re-check a specific broker
dr monitor --broker spokeo
```

### Brokers

```bash
# List all supported brokers
dr brokers list

# Show details for a specific broker
dr brokers info whitepages
```

### Export & PDF Letters

```bash
# Export all data (profile, listings, requests) as JSON
dr export
dr export --output backup.json

# Generate a PDF opt-out letter for mail-in removal
dr letter --broker intelius
dr letter --broker intelius --output letter.pdf
```

The PDF letter is formatted as a US business letter citing CCPA and state
privacy laws. Print it and mail to the broker's postal address.

---

## Supported Brokers

| Broker | Method | Difficulty | Est. Days | Notes |
|--------|--------|------------|-----------|-------|
| **BeenVerified** | Online form | Easy | 1-2 | reCAPTCHA + email verification |
| **FastPeopleSearch** | Online form | Easy | 3 | Cloudflare + reCAPTCHA, visible browser fallback |
| **Intelius** | Online form | Medium | 3 | Also covers TruthFinder + Instant Checkmate. Supports mail-in. |
| **PeopleFinder** | Online form | Medium | 3-9 | Multi-step form with reCAPTCHA |
| **Radaris** | Online form | Medium | 1 | Cloudflare Turnstile |
| **Spokeo** | Email | Medium | 3 | Submit URL + email, then confirm via email |
| **ThatsThem** | Online form | Easy | 5-14 | Standard CAPTCHA |
| **TruePeopleSearch** | Online form | Easy | 3 | Cloudflare protected |
| **USPhonebook** | Online form | Easy | 3 | Cloudflare + reCAPTCHA, email verification |
| **Whitepages** | Online form | Easy | 2 | Phone call verification required |

---

## Workflow Example

Here's a complete walkthrough from start to finish:

```bash
# 1. Set up
pip install -e ".[browser]"
playwright install chromium

# 2. Create your profile with as much detail as possible
dr profile add \
  --first David --last Brown --middle Lee \
  --city "Indian Trail" --state NC \
  --email sohocs509@gmail.com \
  --phone "509-929-9570" \
  --alias "David L Brown" --alias "David E Brown" \
  --relative "Laura Brown" --relative "Jonathan Brown"

# 3. Scan all brokers (takes 2-5 minutes)
dr scan
# Output: Found 55 listing(s)

# 4. Review results -- many won't be you for common names
dr listings list
#  #  | ID       | Broker     | Name Found       | Location         | Confidence | Status
#  1  | a1b2c3d4 | whitepages | David L Brown    | Indian Trail, NC |        70% | discovered
#  2  | e5f6a7b8 | whitepages | David H Brown    | Charlotte, NC    |        35% | discovered
#  3  | c9d0e1f2 | spokeo     | David Brown III  |                  |        30% | discovered
# ...

# 5. Dismiss false positives
dr listings dismiss --below 0.35          # Remove low-confidence matches
dr listings dismiss e5f6 c9d0             # Dismiss specific entries by ID prefix

# 6. Submit opt-out requests
dr remove
# Submitted: 12  Failed: 3  Skipped: 0

# 7. Check your email for verification links (Spokeo, USPhonebook, etc.)

# 8. Check status
dr status
#   Listings found:    55
#   Discovered:        40  (dismissed)
#   Submitted:         12
#   Failed:            3

# 9. Retry failures
dr remove

# 10. After a few days, monitor for completion
dr monitor

# 11. Generate a mail-in letter for brokers that support it
dr letter --broker intelius --output intelius-optout.pdf
# Print and mail to: PO Box 24025, Seattle, WA 98124
```

---

## Confidence Scoring

Each listing gets a confidence score (0-100%) indicating how likely it matches
your profile. The score is computed from:

| Factor | Points | Criteria |
|--------|--------|----------|
| **Name** | +40% | Exact full name match (+30% for partial: first + last in found name) |
| **State** | +20% | State matches any address in profile |
| **City** | +15% | City matches any address in profile |
| **Age** | +15% | Age within 1 year (handles "Age 35" format) |
| **Relatives** | up to +10% | +5% per matching relative name, capped at 10% |

**Tips for better matching:**
- Add your middle name, aliases, and date of birth
- Add multiple addresses (current and past)
- Add relatives -- this significantly improves matching for common names
- Use `dr profile export` / `dr profile import` for bulk editing

---

## State Machine

Every removal request follows this lifecycle:

```
DISCOVERED ──> SUBMITTED ──> PENDING ──> CONFIRMED ──> MONITORING
     |              |            |                          |
     v              v            v                          v
  SKIPPED        FAILED       FAILED                    RE_LISTED
                    |            |                          |
                    v            v                          v
                 (retry)     (retry)                   SUBMITTED
```

| State | Description |
|-------|-------------|
| `DISCOVERED` | Listing found on a broker site |
| `SUBMITTED` | Opt-out request sent to broker |
| `PENDING` | Waiting on broker response |
| `CONFIRMED` | Broker acknowledged removal |
| `MONITORING` | Periodic re-checks for re-listing |
| `RE_LISTED` | Data reappeared after removal |
| `FAILED` | Submission or verification failed (can retry) |
| `SKIPPED` | User chose not to remove (can restore) |

All transitions are enforced. Invalid transitions raise `ValueError`.
Full history with timestamps and reasons is tracked on every request.

---

## Architecture

```
dataremoval/
├── cli.py                  # Typer CLI entry point
├── pdf.py                  # PDF opt-out letter generation (fpdf2)
├── core/
│   ├── models.py           # Profile, Listing, RemovalRequest, state machine
│   ├── database.py         # SQLite persistence (cross-platform paths)
│   └── engine.py           # Async orchestration: scan, remove, monitor
└── brokers/
    ├── __init__.py         # BrokerPlugin ABC, BrokerInfo, BrokerRegistry
    ├── _template.py        # Copy this to add a new broker
    ├── _utils.py           # Shared: confidence, dedup, stealth browser, captcha
    ├── whitepages.py       # 10 broker plugins...
    ├── spokeo.py
    ├── fastpeoplesearch.py
    ├── truepeoplesearch.py
    ├── usphonebook.py
    ├── beenverified.py
    ├── intelius.py
    ├── peoplefinder.py
    ├── radaris.py
    └── thatsthem.py
```

### Key Design Decisions

**Plugin system.** Each broker is a Python module implementing `BrokerPlugin`.
Drop a new file in `brokers/`, implement `search()` and `submit_opt_out()`,
call `register_broker()`, and it auto-discovers on import. The CLI, database,
and state machine all work automatically.

**Local-first.** SQLite database on your machine. No server, no accounts,
no cloud sync. Data stays local unless you're sending an opt-out to a broker site.

**Async engine.** Broker searches and opt-outs run concurrently using asyncio
with per-broker rate limiting via semaphores (default: 5 concurrent searches,
3 concurrent opt-outs).

**Stealth browser automation.** Uses `playwright-stealth` to bypass bot detection
on Cloudflare-protected sites. Falls back to a visible browser window when
CAPTCHAs require manual solving.

**Cross-platform.** Uses `platformdirs` for OS-native data paths.
Works on Linux, macOS, and Windows without modification.

### Data Storage

All data is stored locally in SQLite:

| OS | Database Path |
|----|---------------|
| Linux | `~/.local/share/data-removal/data.db` |
| macOS | `~/Library/Application Support/data-removal/data.db` |
| Windows | `%LOCALAPPDATA%\data-removal\data-removal\data.db` |

Reset the database:
```bash
make reset-db   # or: rm ~/.local/share/data-removal/data.db
```

---

## Adding a New Broker

1. Copy the template:
   ```bash
   cp dataremoval/brokers/_template.py dataremoval/brokers/newsite.py
   ```

2. Implement the three required methods:

```python
from dataremoval.brokers import (
    BrokerInfo, BrokerPlugin, Difficulty, OptOutMethod, register_broker,
)
from dataremoval.brokers._utils import (
    HAS_PLAYWRIGHT, check_url_status, compute_confidence,
    deduplicate, launch_browser, stealth_playwright,
)
from dataremoval.core.models import Listing, Profile


class NewSitePlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="newsite",
            name="NewSite",
            url="https://newsite.com",
            category="people_search",
            opt_out_method=OptOutMethod.ONLINE_FORM,
            opt_out_url="https://newsite.com/optout",
            difficulty=Difficulty.EASY,
            expected_days=3,
            recheck_days=90,
            notes="Description of opt-out process.",
        )

    async def search(self, profile: Profile) -> list[Listing]:
        """Search the site for listings matching the profile."""
        if not HAS_PLAYWRIGHT:
            return []

        listings = []
        async with stealth_playwright() as pw:
            browser = await launch_browser(pw, headless=True)
            try:
                page = await browser.new_page()
                # Navigate, extract data, build Listing objects
                # Use compute_confidence() for scoring
                # Use deduplicate() before returning
            finally:
                await browser.close()

        return deduplicate(listings)

    async def submit_opt_out(self, listing: Listing) -> bool:
        """Submit an opt-out request. Return True on success."""
        # Automate the opt-out form
        return False

    async def check_status(self, listing: Listing) -> str:
        """Check if a listing URL still resolves."""
        return await check_url_status(listing.url)


register_broker(NewSitePlugin())
```

3. Add tests in `tests/test_brokers.py` following the existing patterns.

4. Run `dr brokers list` to verify your plugin loaded.

---

## Configuration

`dr` currently uses no configuration files. All settings are passed via
CLI flags. Key environment behaviors:

- **Database path**: Determined by `platformdirs` based on OS
- **Browser**: Chromium via Playwright (must be installed separately)
- **Concurrency**: 5 concurrent searches, 3 concurrent opt-outs (hardcoded in engine)
- **Captcha timeout**: 300 seconds (5 minutes) for manual solving
- **User agent**: Chrome 124 on Linux

---

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test files
pytest tests/test_core.py -v      # Models, state machine, database
pytest tests/test_brokers.py -v   # Broker plugins (URL builders, selectors)
pytest tests/test_utils.py -v     # Shared utilities (confidence, dedup)
pytest tests/test_pdf.py -v       # PDF generation (requires fpdf2)

# Lint
ruff check dataremoval/ tests/
ruff format --check dataremoval/ tests/
mypy dataremoval/ --ignore-missing-imports
```

CI runs on every push and PR against `main`:
- **9 test matrix**: Python 3.11/3.12/3.13 x Ubuntu/macOS/Windows
- **Lint job**: ruff check + ruff format + mypy

---

## Contributing

1. Fork the repo and create a feature branch
2. Add or modify broker plugins in `dataremoval/brokers/`
3. Add tests in `tests/`
4. Ensure `pytest tests/ -v` passes and `ruff check` is clean
5. Open a pull request

When adding a new broker, include:
- The broker's actual CSS selectors (inspect the live DOM)
- Correct URL patterns for search and opt-out
- Notes about bot protection (Cloudflare, reCAPTCHA, etc.)
- Expected opt-out processing time

---

## License

Private. All rights reserved.

---

## About

Built and maintained by **[Enthropic Data](https://enthropicdata.com)** — an AI product studio based in Weddington, NC.

Follow our work at [enthropicdata.com](https://enthropicdata.com) or explore our other open source projects at [github.com/Enthropic-Data-LLC](https://github.com/Enthropic-Data-LLC).

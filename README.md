# dr — Data Removal CLI

Remove your personal data from people-search and data broker sites.

Runs on **Linux**, **macOS**, and **Windows**. Requires Python 3.11+.

## Setup

### Cross-platform (recommended)

```bash
python3 bootstrap.py            # or: python bootstrap.py on Windows
```

The setup script auto-detects your OS, installs system dependencies if needed,
creates a virtual environment, installs the project, and runs tests.

Options:
```bash
python3 bootstrap.py --check     # Check prerequisites only
python3 bootstrap.py --browser   # Also install Playwright for form automation
```

### Manual setup

```bash
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest tests/ -v
```

### Dev commands

Linux / macOS (make):
```bash
make setup          # Create venv + install
make test           # Run tests
make lint           # Ruff + mypy
make fmt            # Auto-format
make browser-deps   # Install Playwright
make reset-db       # Delete local database
```

Windows (dev.bat):
```cmd
dev setup
dev test
dev lint
dev fmt
dev browser
dev reset-db
```

## Quick Start

```bash
# 1. Create a profile
dr profile add --first John --last Doe --city Portland --state OR

# 2. Scan broker sites
dr scan

# 3. Submit opt-out requests
dr remove

# 4. Check status
dr status

# 5. Re-check for re-listings
dr monitor
```

## Commands

```
dr profile add         Add a new profile to protect
dr profile list        List all profiles
dr profile show <id>   Show profile details
dr profile delete <id> Delete a profile

dr scan                Scan brokers for your data
dr remove              Submit opt-out requests
dr status              Show removal status dashboard
dr monitor             Re-check for re-listings

dr brokers list        List all supported broker sites
dr brokers info <id>   Show details for a broker

dr export              Export all data as JSON
```

## Data Storage

All data stays local in a SQLite database:

| OS | Path |
|----|------|
| Linux | `~/.local/share/data-removal/data.db` |
| macOS | `~/Library/Application Support/data-removal/data.db` |
| Windows | `%LOCALAPPDATA%\data-removal\data-removal\data.db` |

## Architecture

```
dataremoval/
├── cli.py                  # Typer CLI — all commands
├── core/
│   ├── models.py           # Profile, Listing, RemovalRequest (state machine)
│   ├── database.py         # SQLite persistence (cross-platform paths)
│   └── engine.py           # Orchestration — scan, remove, monitor
└── brokers/
    ├── __init__.py          # BrokerPlugin base class + registry
    ├── _template.py         # Copy this to add a new broker
    ├── whitepages.py        # Whitepages plugin
    ├── spokeo.py            # Spokeo plugin
    └── truepeoplesearch.py  # TruePeopleSearch plugin
```

### Key Design Decisions

**Plugin system.** Each broker is a Python module implementing `BrokerPlugin`.
Drop a new file in `brokers/`, implement `search()` and `submit_opt_out()`,
and it auto-registers. The CLI, database, and state machine all work automatically.

**State machine.** Every removal request flows through:
`DISCOVERED → SUBMITTED → PENDING → CONFIRMED → MONITORING → (RE_LISTED → SUBMITTED)`.
Invalid transitions raise errors. Full history is tracked.

**Local-first.** SQLite database on your machine. No server, no accounts,
no data leaves your machine unless you're talking to a broker site.

**Cross-platform.** Uses `platformdirs` for OS-native data paths. No hardcoded
Unix paths, no bash-specific scripts in the critical path.

**Async engine.** Broker searches and opt-outs run concurrently with per-broker
rate limiting via asyncio semaphores.

## Adding a New Broker

1. Copy `dataremoval/brokers/_template.py` → `dataremoval/brokers/newsite.py`
2. Fill in `info()` with the site's metadata
3. Implement `search()` — HTTP requests + HTML parsing
4. Implement `submit_opt_out()` — form submission or email
5. Done. The plugin auto-registers on import.

```python
from dataremoval.brokers import (
    BrokerInfo, BrokerPlugin, Difficulty, OptOutMethod, register_broker,
)
from dataremoval.core.models import Listing, Profile

class NewSitePlugin(BrokerPlugin):
    def info(self) -> BrokerInfo:
        return BrokerInfo(
            id="newsite",
            name="NewSite",
            url="https://newsite.com",
            opt_out_url="https://newsite.com/optout",
            difficulty=Difficulty.EASY,
            expected_days=2,
            recheck_days=90,
        )

    async def search(self, profile: Profile) -> list[Listing]:
        return []

    async def submit_opt_out(self, listing: Listing) -> bool:
        return False

register_broker(NewSitePlugin())
```

## Testing

```bash
pytest tests/ -v
```

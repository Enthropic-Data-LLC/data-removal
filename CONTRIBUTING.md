# Contributing

## Getting Started

```bash
git clone git@github.com:enthropicdata/data-removal.git
cd data-removal
python3 bootstrap.py       # or: make setup
```

## Workflow

1. Create a branch from `main`:
   ```bash
   git checkout -b feature/add-radaris-broker
   ```
2. Make your changes
3. Run tests: `make test` (or `dev test` on Windows)
4. Push and open a PR against `main`
5. Get one approval from a maintainer
6. CI must pass (tests on Linux/macOS/Windows + lint)
7. Squash-merge

### Branch naming

- `feature/description` — new functionality
- `fix/description` — bug fixes
- `broker/site-name` — new broker plugins

## Adding a Broker Plugin

This is the most common contribution. See the [README](README.md#adding-a-new-broker) for the quick version. In more detail:

1. **File an issue** using the "New Broker Plugin" template
2. **Copy the template:**
   ```bash
   cp dataremoval/brokers/_template.py dataremoval/brokers/newsite.py
   ```
3. **Fill in `info()`** with metadata from BROKERS.md or your own research
4. **Implement `search()`** — this is the hard part:
   - Use `httpx` for simple HTTP requests
   - Use Playwright if the site requires JavaScript
   - Parse results and return `Listing` objects
   - Use `profile.search_variants()` for name/location combos
5. **Implement `submit_opt_out()`** — site-specific form/email/API
6. **Add tests** in `tests/test_brokers.py`
7. **Update BROKERS.md** if the site isn't listed yet

## Code Style

- Format with `ruff format`
- Lint with `ruff check`
- Type hints on all function signatures
- No OS-specific hardcoded paths (use `platformdirs`)
- Async for all broker I/O

## Tests

```bash
make test           # run all tests
make test-watch     # re-run on file change
```

Tests must pass on all three platforms (CI runs Linux, macOS, Windows).

## Commit Messages

Keep them short and descriptive:
```
Add Radaris broker plugin
Fix whitepages opt-out URL parsing
Update CI to test Python 3.13
```

No need for conventional commits or elaborate prefixes at this stage.

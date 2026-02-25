"""
Data Removal CLI

Usage:
    dr profile add         Add a new profile to protect
    dr profile list        List all profiles
    dr profile show <id>   Show profile details
    dr profile delete <id> Delete a profile

    dr scan [--profile ID] [--broker ID]  Scan brokers for your data
    dr remove [--profile ID]              Submit opt-out requests
    dr status [--profile ID]              Show removal status dashboard
    dr monitor [--profile ID]             Re-check for re-listings

    dr brokers list        List all supported broker sites
    dr brokers info <id>   Show details for a broker

    dr export [--profile ID]  Export all data as JSON

    dr letter --broker ID     Generate PDF opt-out letter for mailing
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from dataremoval.brokers import registry
from dataremoval.core.database import Database
from dataremoval.core.engine import Engine
from dataremoval.core.models import Address, Profile, RemovalState

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="dr",
    help="Remove your personal data from broker sites.",
    no_args_is_help=True,
)
profile_app = typer.Typer(help="Manage profiles to protect.")
broker_app = typer.Typer(help="Browse supported broker sites.")
app.add_typer(profile_app, name="profile")
app.add_typer(broker_app, name="brokers")

console = Console()

# Ensure plugins are loaded
registry.discover()


def _db() -> Database:
    return Database()


def _default_profile(db: Database, profile_id: str | None) -> Profile:
    """Resolve profile — use given ID or the first (only) profile."""
    if profile_id:
        p = db.get_profile(profile_id)
        if not p:
            console.print(f"[red]Profile '{profile_id}' not found.[/red]")
            raise typer.Exit(1)
        return p
    profiles = db.list_profiles()
    if not profiles:
        console.print("[red]No profiles found. Create one with:[/red]  dr profile add")
        raise typer.Exit(1)
    if len(profiles) > 1:
        console.print("[yellow]Multiple profiles found — specify with --profile ID[/yellow]")
        for p in profiles:
            console.print(f"  {p.id}  {p.full_name}")
        raise typer.Exit(1)
    return profiles[0]


# ---------------------------------------------------------------------------
# Profile commands
# ---------------------------------------------------------------------------


@profile_app.command("add")
def profile_add(
    first: str = typer.Option(..., "--first", "-f", prompt="First name"),
    last: str = typer.Option(..., "--last", "-l", prompt="Last name"),
    middle: str = typer.Option("", "--middle", "-m"),
    dob: str = typer.Option("", "--dob", help="Date of birth (YYYY-MM-DD)"),
    city: str = typer.Option("", "--city"),
    state: str = typer.Option("", "--state"),
    phone: str = typer.Option("", "--phone"),
    email: str = typer.Option("", "--email"),
):
    """Add a new profile to protect."""
    addresses = []
    if city or state:
        addresses.append(Address(city=city, state=state, current=True))

    phones = [phone] if phone else []
    emails = [email] if email else []

    profile = Profile(
        first_name=first,
        last_name=last,
        middle_name=middle,
        date_of_birth=dob,
        addresses=addresses,
        phone_numbers=phones,
        email_addresses=emails,
    )

    db = _db()
    db.save_profile(profile)
    db.close()

    console.print(
        Panel(
            f"[bold green]Profile created[/bold green]\n\n"
            f"  ID:   {profile.id}\n"
            f"  Name: {profile.full_name}\n"
            f"  City: {city or '—'}  State: {state or '—'}\n\n"
            f"Next: [bold]dr scan --profile {profile.id}[/bold]",
            title="✓ Profile Added",
        )
    )


@profile_app.command("list")
def profile_list():
    """List all profiles."""
    db = _db()
    profiles = db.list_profiles()
    db.close()

    if not profiles:
        console.print("[dim]No profiles yet.[/dim]  Run: dr profile add")
        return

    table = Table(title="Profiles")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Location")
    table.add_column("Created")

    for p in profiles:
        loc = str(p.addresses[0]) if p.addresses else "—"
        table.add_row(p.id, p.full_name, loc, p.created_at[:10])

    console.print(table)


@profile_app.command("show")
def profile_show(profile_id: str = typer.Argument(..., help="Profile ID")):
    """Show detailed profile info."""
    db = _db()
    p = db.get_profile(profile_id)
    if not p:
        console.print(f"[red]Profile '{profile_id}' not found.[/red]")
        raise typer.Exit(1)

    stats = db.stats(profile_id)
    db.close()

    lines = [
        f"  [bold]Name:[/bold]      {p.full_name}",
        f"  [bold]DOB:[/bold]       {p.date_of_birth or '—'}",
        f"  [bold]Aliases:[/bold]   {', '.join(p.aliases) or '—'}",
        f"  [bold]Addresses:[/bold] {'; '.join(str(a) for a in p.addresses) or '—'}",
        f"  [bold]Phones:[/bold]    {', '.join(p.phone_numbers) or '—'}",
        f"  [bold]Emails:[/bold]    {', '.join(p.email_addresses) or '—'}",
        f"  [bold]Relatives:[/bold] {', '.join(p.relatives) or '—'}",
        "",
        f"  [bold]Listings found:[/bold]  {stats['listings']}",
        f"  [bold]Requests:[/bold]        {json.dumps(stats['requests_by_state']) or '—'}",
    ]
    console.print(Panel("\n".join(lines), title=f"Profile: {profile_id}"))


@profile_app.command("delete")
def profile_delete(
    profile_id: str = typer.Argument(..., help="Profile ID"),
    force: bool = typer.Option(False, "--force", "-y", help="Skip confirmation"),
):
    """Delete a profile and all associated data."""
    db = _db()
    p = db.get_profile(profile_id)
    if not p:
        console.print(f"[red]Profile '{profile_id}' not found.[/red]")
        raise typer.Exit(1)

    if not force:
        confirm = typer.confirm(f"Delete profile '{p.full_name}' ({profile_id})?")
        if not confirm:
            raise typer.Abort()

    db.delete_profile(profile_id)
    db.close()
    console.print(f"[green]Profile {profile_id} deleted.[/green]")


# ---------------------------------------------------------------------------
# Scan / Remove / Monitor
# ---------------------------------------------------------------------------


@app.command()
def scan(
    profile_id: str | None = typer.Option(None, "--profile", "-p"),
    broker: str | None = typer.Option(None, "--broker", "-b", help="Specific broker ID"),
):
    """Scan broker sites for your personal data."""
    db = _db()
    profile = _default_profile(db, profile_id)
    broker_ids = [broker] if broker else None

    engine = Engine(db, on_event=_log_event)

    console.print(f"[bold]Scanning for:[/bold] {profile.full_name}")
    console.print(f"[bold]Brokers:[/bold]     {broker or 'all'} ({len(registry)} loaded)\n")

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("Scanning broker sites...", total=None)
        listings = asyncio.run(engine.scan(profile, broker_ids=broker_ids))
        progress.update(task, completed=True)

    db.close()

    if listings:
        table = Table(title=f"Found {len(listings)} listing(s)")
        table.add_column("Broker", style="cyan")
        table.add_column("Name Found")
        table.add_column("Location")
        table.add_column("URL")

        for listing in listings:
            table.add_row(
                listing.broker_id, listing.found_name, listing.found_location, listing.url
            )
        console.print(table)
        console.print(f"\nNext: [bold]dr remove --profile {profile.id}[/bold]")
    else:
        console.print(
            "[dim]No listings found (broker search not yet implemented for all sites).[/dim]"
        )


@app.command()
def remove(
    profile_id: str | None = typer.Option(None, "--profile", "-p"),
    broker: str | None = typer.Option(None, "--broker", "-b"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would be submitted"),
):
    """Submit opt-out requests for discovered listings."""
    db = _db()
    profile = _default_profile(db, profile_id)
    broker_ids = [broker] if broker else None

    # Show what's pending
    requests = db.get_requests(profile_id=profile.id)
    actionable = [
        r
        for r in requests
        if r.state in (RemovalState.DISCOVERED, RemovalState.FAILED, RemovalState.RE_LISTED)
        and (broker_ids is None or r.broker_id in broker_ids)
    ]

    if not actionable:
        console.print("[dim]No pending opt-outs. Run[/dim] dr scan [dim]first.[/dim]")
        db.close()
        return

    console.print(f"[bold]{len(actionable)} opt-out(s) to submit:[/bold]\n")
    for r in actionable:
        console.print(f"  • {r.broker_id} — {r.state.value}")

    if dry_run:
        console.print("\n[yellow]Dry run — no requests submitted.[/yellow]")
        db.close()
        return

    engine = Engine(db, on_event=_log_event)
    results = asyncio.run(engine.remove(profile.id, broker_ids=broker_ids))
    db.close()

    console.print(
        f"\n[green]Submitted: {results['submitted']}[/green]  "
        f"[red]Failed: {results['failed']}[/red]  "
        f"[dim]Skipped: {results['skipped']}[/dim]"
    )


@app.command()
def status(
    profile_id: str | None = typer.Option(None, "--profile", "-p"),
):
    """Show removal status dashboard."""
    db = _db()
    profile = _default_profile(db, profile_id)
    requests = db.get_requests(profile_id=profile.id)
    stats = db.stats(profile.id)
    db.close()

    if not requests:
        console.print("[dim]No data yet. Run[/dim] dr scan [dim]first.[/dim]")
        return

    # Summary panel
    by_state = stats["requests_by_state"]
    summary_lines = [
        f"  Listings found:    {stats['listings']}",
        f"  Discovered:        {by_state.get('discovered', 0)}",
        f"  Submitted:         {by_state.get('submitted', 0)}",
        f"  Pending:           {by_state.get('pending', 0)}",
        f"  Confirmed:         {by_state.get('confirmed', 0)}",
        f"  Monitoring:        {by_state.get('monitoring', 0)}",
        f"  Re-listed:         {by_state.get('re_listed', 0)}",
        f"  Failed:            {by_state.get('failed', 0)}",
    ]
    console.print(Panel("\n".join(summary_lines), title=f"Status: {profile.full_name}"))

    # Detail table
    table = Table()
    table.add_column("Broker", style="cyan")
    table.add_column("State")
    table.add_column("Attempts")
    table.add_column("Submitted")
    table.add_column("Confirmed")

    state_colors = {
        "discovered": "white",
        "submitted": "yellow",
        "pending": "yellow",
        "confirmed": "green",
        "monitoring": "green",
        "re_listed": "red",
        "failed": "red",
        "skipped": "dim",
    }

    for r in requests:
        color = state_colors.get(r.state.value, "white")
        table.add_row(
            r.broker_id,
            f"[{color}]{r.state.value}[/{color}]",
            str(r.attempts),
            (r.submitted_at or "—")[:10],
            (r.confirmed_at or "—")[:10],
        )

    console.print(table)


@app.command()
def monitor(
    profile_id: str | None = typer.Option(None, "--profile", "-p"),
    broker: str | None = typer.Option(None, "--broker", "-b"),
):
    """Re-check confirmed removals for re-listings."""
    db = _db()
    profile = _default_profile(db, profile_id)
    broker_ids = [broker] if broker else None

    engine = Engine(db, on_event=_log_event)
    results = asyncio.run(engine.monitor(profile.id, broker_ids=broker_ids))
    db.close()

    console.print(
        f"Checked: {results['checked']}  "
        f"[green]Still removed: {results['still_removed']}[/green]  "
        f"[red]Re-listed: {results['re_listed']}[/red]  "
        f"[dim]Unknown: {results['unknown']}[/dim]"
    )


# ---------------------------------------------------------------------------
# Broker commands
# ---------------------------------------------------------------------------


@broker_app.command("list")
def brokers_list():
    """List all supported broker sites."""
    plugins = registry.all()
    if not plugins:
        console.print("[dim]No broker plugins loaded.[/dim]")
        return

    table = Table(title=f"Supported Brokers ({len(plugins)})")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Method")
    table.add_column("Difficulty")
    table.add_column("Est. Days")

    for p in sorted(plugins, key=lambda p: p.info().name):
        info = p.info()
        diff_color = {"easy": "green", "medium": "yellow", "hard": "red"}
        color = diff_color.get(info.difficulty.value, "white")
        table.add_row(
            info.id,
            info.name,
            info.category,
            info.opt_out_method.value,
            f"[{color}]{info.difficulty.value}[/{color}]",
            str(info.expected_days),
        )

    console.print(table)


@broker_app.command("info")
def broker_info(broker_id: str = typer.Argument(..., help="Broker ID")):
    """Show detailed info about a broker."""
    plugin = registry.get(broker_id)
    if not plugin:
        console.print(f"[red]Broker '{broker_id}' not found.[/red]")
        console.print(f"Available: {', '.join(registry.ids())}")
        raise typer.Exit(1)

    info = plugin.info()
    lines = [
        f"  [bold]Name:[/bold]       {info.name}",
        f"  [bold]URL:[/bold]        {info.url}",
        f"  [bold]Category:[/bold]   {info.category}",
        f"  [bold]Opt-out:[/bold]    {info.opt_out_method.value}",
        f"  [bold]Opt-out URL:[/bold]{info.opt_out_url}",
        f"  [bold]Difficulty:[/bold] {info.difficulty.value}",
        f"  [bold]Est. time:[/bold]  {info.expected_days} day(s)",
        f"  [bold]Re-check:[/bold]   every {info.recheck_days} days",
    ]
    if info.notes:
        lines.append(f"\n  [dim]{info.notes}[/dim]")

    console.print(Panel("\n".join(lines), title=f"Broker: {info.id}"))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@app.command()
def export(
    profile_id: str | None = typer.Option(None, "--profile", "-p"),
    output: str = typer.Option("-", "--output", "-o", help="Output file (- for stdout)"),
):
    """Export all data as JSON."""
    db = _db()
    profile = _default_profile(db, profile_id)
    listings = db.get_listings(profile_id=profile.id)
    requests = db.get_requests(profile_id=profile.id)
    db.close()

    data = {
        "profile": profile.model_dump(),
        "listings": [item.model_dump() for item in listings],
        "requests": [r.model_dump() for r in requests],
    }

    payload = json.dumps(data, indent=2, default=str)

    if output == "-":
        console.print(payload)
    else:
        Path(output).write_text(payload)
        console.print(f"[green]Exported to {output}[/green]")


# ---------------------------------------------------------------------------
# Letter (PDF opt-out)
# ---------------------------------------------------------------------------


@app.command()
def letter(
    profile_id: str | None = typer.Option(None, "--profile", "-p"),
    broker_id: str = typer.Option(..., "--broker", "-b", help="Broker ID"),
    listing_id: str = typer.Option(
        "", "--listing", "-l", help="Listing ID (uses first if omitted)"
    ),
    output: str = typer.Option("", "--output", "-o", help="Output PDF path"),
):
    """Generate a PDF opt-out letter for mail-in removal."""
    from dataremoval.pdf import generate_opt_out_letter

    db = _db()
    profile = _default_profile(db, profile_id)

    plugin = registry.get(broker_id)
    if not plugin:
        console.print(f"[red]Broker '{broker_id}' not found.[/red]")
        raise typer.Exit(1)

    info = plugin.info()
    if not info.mail_address:
        console.print(f"[red]Broker '{broker_id}' does not support mail-in opt-out.[/red]")
        raise typer.Exit(1)

    # Find the listing
    listings = db.get_listings(profile_id=profile.id, broker_id=broker_id)
    if not listings:
        console.print(
            f"[red]No listings found for broker '{broker_id}'.[/red]\n"
            f"Run [bold]dr scan --broker {broker_id}[/bold] first."
        )
        raise typer.Exit(1)

    if listing_id:
        matches = [li for li in listings if li.id == listing_id]
        if not matches:
            console.print(f"[red]Listing '{listing_id}' not found.[/red]")
            raise typer.Exit(1)
        listing = matches[0]
    else:
        listing = listings[0]

    db.close()

    if not output:
        output = f"opt-out-{info.id}-{listing.id[:8]}.pdf"

    out_path = generate_opt_out_letter(
        profile=profile,
        listing=listing,
        broker_name=info.name,
        broker_address=info.mail_address,
        output_path=output,
    )

    console.print(
        Panel(
            f"[bold green]Letter generated[/bold green]\n\n"
            f"  File:    {out_path}\n"
            f"  Broker:  {info.name}\n"
            f"  Mail to: {info.mail_address}\n\n"
            f"Print and mail this letter to the address above.",
            title="PDF Opt-Out Letter",
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_event(*args):
    """Simple event logger for engine callbacks."""
    if len(args) >= 2:
        console.print(f"  [dim]{args[0]}:[/dim] {' '.join(str(a) for a in args[1:])}")


def main():
    app()

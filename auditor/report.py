from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from auditor.loader import Claim, ClaimStatus

console = Console()

_ICONS = {
    ClaimStatus.verified: "[green]✓[/green]",
    ClaimStatus.failed: "[red]✗[/red]",
    ClaimStatus.blocked: "[yellow]⊘[/yellow]",
    ClaimStatus.unverifiable: "[dim]?[/dim]",
    ClaimStatus.not_started: "[dim]-[/dim]",
    ClaimStatus.in_progress: "[blue]~[/blue]",
}


def write_report(claims: list[Claim], evidence_records: list[dict], run_id: str, output_path: Path) -> None:
    report = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": _summary(claims),
        "claims": [
            {
                "id": c.id,
                "description": c.description,
                "type": c.type,
                "status": c.status,
                "evidence": next((e for e in evidence_records if e["claim_id"] == c.id), None),
            }
            for c in claims
        ],
    }
    output_path.write_text(json.dumps(report, indent=2, default=str))


def print_summary(claims: list[Claim], evidence_records: list[dict], run_id: str) -> None:
    s = _summary(claims)

    console.print(f"\n[bold]Run:[/bold] {run_id}")
    console.print(f"[bold]Total:[/bold] {s['total']}  "
                  f"[green]Verified: {s['verified']}[/green]  "
                  f"[red]Failed: {s['failed']}[/red]  "
                  f"[yellow]Blocked: {s['blocked']}[/yellow]  "
                  f"[dim]Unverifiable: {s['unverifiable']}[/dim]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Status", width=4)
    table.add_column("ID", style="dim")
    table.add_column("Description")
    table.add_column("Confidence", width=10)

    for c in claims:
        ev = next((e for e in evidence_records if e.get("claim_id") == c.id), {})
        confidence = ev.get("confidence", "")
        table.add_row(_ICONS[c.status], c.id, c.description, confidence)

    console.print(table)


def _summary(claims: list[Claim]) -> dict:
    statuses = [c.status for c in claims]
    return {
        "total": len(claims),
        "verified": statuses.count(ClaimStatus.verified),
        "failed": statuses.count(ClaimStatus.failed),
        "blocked": statuses.count(ClaimStatus.blocked),
        "unverifiable": statuses.count(ClaimStatus.unverifiable),
    }

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from auditor.loader import Claim, ClaimStatus, FlowFile

console = Console()

_ICONS = {
    ClaimStatus.verified:     "[green]✓[/green]",
    ClaimStatus.failed:       "[red]✗[/red]",
    ClaimStatus.blocked:      "[yellow]⊘[/yellow]",
    ClaimStatus.unverifiable: "[dim]?[/dim]",
    ClaimStatus.not_started:  "[dim]-[/dim]",
    ClaimStatus.in_progress:  "[blue]~[/blue]",
}


def write_report(flow_file: FlowFile, evidence_records: list[dict], run_id: str, output_path: Path) -> None:
    ev_map = {e["claim_id"]: e for e in evidence_records}
    flows_out = []
    for flow in flow_file.flows:
        steps_out = []
        for step in flow.steps:
            claims_out = [
                {
                    "id": c.id,
                    "description": c.description,
                    "type": c.type,
                    "status": c.status,
                    "evidence": ev_map.get(c.id),
                }
                for c in step.claims
            ]
            steps_out.append({
                "id": step.id,
                "goal": step.goal,
                "status": step.status,
                "claims": claims_out,
            })
        flows_out.append({
            "id": flow.id,
            "description": flow.description,
            "steps": steps_out,
        })

    all_claims = flow_file.all_claims
    report = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": _summary(all_claims),
        "flows": flows_out,
    }
    output_path.write_text(json.dumps(report, indent=2, default=str))


def print_summary(claims: list[Claim], evidence_records: list[dict], run_id: str) -> None:
    s = _summary(claims)

    console.print(Rule(style="dim"))
    console.print(f"\n[bold]Run:[/bold] {run_id}")
    console.print(
        f"[bold]Total:[/bold] {s['total']}  "
        f"[green]Verified: {s['verified']}[/green]  "
        f"[red]Failed: {s['failed']}[/red]  "
        f"[yellow]Blocked: {s['blocked']}[/yellow]  "
        f"[dim]Unverifiable: {s['unverifiable']}[/dim]\n"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Sta…", width=4)
    table.add_column("ID", style="dim")
    table.add_column("Description")
    table.add_column("Confidence", width=10)

    ev_map = {e["claim_id"]: e for e in evidence_records}
    for c in claims:
        ev = ev_map.get(c.id, {})
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

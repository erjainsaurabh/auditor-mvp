from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from flowprobe.loader import FlowFile, Step, StepStatus
from flowprobe.logger import get_logger

log = get_logger(__name__)

console = Console()

_ICONS = {
    StepStatus.verified:     "[green]✓[/green]",
    StepStatus.failed:       "[red]✗[/red]",
    StepStatus.blocked:      "[yellow]⊘[/yellow]",
    StepStatus.unverifiable: "[dim]?[/dim]",
    StepStatus.not_started:  "[dim]-[/dim]",
    StepStatus.in_progress:  "[blue]~[/blue]",
}


def write_report(flow_file: FlowFile, evidence_records: list[dict], run_id: str, output_path: Path) -> None:
    log.info("write_report — run_id=%s flows=%d evidence_records=%d", run_id, len(flow_file.flows), len(evidence_records))
    ev_map = {e["claim_id"]: e for e in evidence_records}
    log.debug("evidence claim_ids in map: %s", list(ev_map.keys()))
    flows_out = []
    for flow in flow_file.flows:
        log.debug("processing flow id=%s", flow.id)
        tcs_out = []
        # Attribute name guard: log what the flow object actually has so
        # mismatches between report.py and loader.py are immediately visible.
        tc_list = getattr(flow, "test_conditions", None)
        if tc_list is None:
            log.error(
                "flow '%s' has no attribute 'test_conditions' — "
                "available attrs: %s",
                flow.id,
                [a for a in dir(flow) if not a.startswith("_")],
            )
            raise AttributeError(
                f"Flow '{flow.id}' has no attribute 'test_conditions'. "
                f"Check that report.py matches the current loader.py schema."
            )
        for tc in tc_list:
            log.debug("  processing test_condition id=%s", tc.id)
            step_list = getattr(tc, "steps", None)
            if step_list is None:
                log.error(
                    "test_condition '%s' has no attribute 'steps' — "
                    "available attrs: %s",
                    tc.id,
                    [a for a in dir(tc) if not a.startswith("_")],
                )
                raise AttributeError(
                    f"TestCondition '{tc.id}' has no attribute 'steps'. "
                    f"Check that report.py matches the current loader.py schema."
                )
            steps_out = []
            for s in step_list:
                log.debug("    step id=%s status=%s", s.id, getattr(s, "status", "?"))
                ev = ev_map.get(s.id) or {}
                artifact = ev.get("artifact")
                evidence = {k: v for k, v in ev.items() if k != "artifact"}
                steps_out.append({
                    "id": s.id,
                    "description": s.description,
                    "type": getattr(s, "type", None),
                    "status": s.status.value,
                    "fingerprint_status": ev.get("fingerprint_status", "none"),
                    "evidence": evidence,
                    "artifact": artifact,
                })
            tcs_out.append({
                "id": tc.id,
                "goal": tc.goal,
                "status": tc.status.value,
                "steps": steps_out,
            })
        flows_out.append({
            "id": flow.id,
            "description": flow.description,
            "test_conditions": tcs_out,
        })

    all_steps = flow_file.all_steps
    log.debug("all_steps count=%d", len(all_steps))
    summary = _summary(all_steps)
    log.info("summary — %s", summary)
    report = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            **summary,
            "fingerprint_hits": sum(1 for e in evidence_records if e.get("fingerprint_status") == "hit"),
            "fingerprint_misses": sum(1 for e in evidence_records if e.get("fingerprint_status") == "miss"),
            "fingerprint_none": sum(1 for e in evidence_records if e.get("fingerprint_status") == "none"),
        },
        "flows": flows_out,
    }
    output_path.write_text(json.dumps(report, indent=2, default=str))
    log.info("report written to %s (%d bytes)", output_path, output_path.stat().st_size)


_FP_ICONS = {
    "hit":  "[cyan]⚡hit[/cyan]",
    "miss": "[yellow]⚡miss[/yellow]",
    "none": "[dim]—[/dim]",
}


def print_summary(steps: list[Step], evidence_records: list[dict], run_id: str) -> None:
    s = _summary(steps)

    console.print(Rule(style="dim"))
    console.print(f"\n[bold]Run:[/bold] {run_id}")
    console.print(
        f"[bold]Total:[/bold] {s['total']}  "
        f"[green]Verified: {s['verified']}[/green]  "
        f"[red]Failed: {s['failed']}[/red]  "
        f"[yellow]Blocked: {s['blocked']}[/yellow]  "
        f"[dim]Unverifiable: {s['unverifiable']}[/dim]\n"
    )

    fp_hits = sum(1 for e in evidence_records if e.get("fingerprint_status") == "hit")
    fp_misses = sum(1 for e in evidence_records if e.get("fingerprint_status") == "miss")
    fp_none = sum(1 for e in evidence_records if e.get("fingerprint_status") == "none")
    console.print(
        f"[bold]Fingerprint:[/bold] "
        f"[cyan]⚡ hit: {fp_hits}[/cyan]  "
        f"[yellow]⚡ miss: {fp_misses}[/yellow]  "
        f"[dim]none: {fp_none}[/dim]\n"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Sta…", width=4)
    table.add_column("ID", style="dim")
    table.add_column("Description")
    table.add_column("Confidence", width=10)
    table.add_column("Fingerprint", width=10)

    ev_map = {e["claim_id"]: e for e in evidence_records}
    for s in steps:
        ev = ev_map.get(s.id, {})
        confidence = ev.get("confidence", "")
        fp_status = ev.get("fingerprint_status", "none")
        fp_cell = _FP_ICONS.get(fp_status, "[dim]—[/dim]")
        table.add_row(_ICONS[s.status], s.id, s.description, confidence, fp_cell)

    console.print(table)


def _summary(steps: list[Step]) -> dict:
    statuses = [s.status for s in steps]
    return {
        "total": len(steps),
        "verified": statuses.count(StepStatus.verified),
        "failed": statuses.count(StepStatus.failed),
        "blocked": statuses.count(StepStatus.blocked),
        "unverifiable": statuses.count(StepStatus.unverifiable),
    }

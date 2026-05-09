from __future__ import annotations

import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

import yaml
from rich.console import Console

from auditor.agent import run_step
from auditor.fingerprint import FingerprintRouter, FingerprintStore
from auditor.graph import build_step_graph, cascade_step_failure, mark_steps_blocked, step_execution_order
from auditor.llm_client import LLMClient
from auditor.loader import StepStatus, load_flows
from auditor.report import print_summary, write_report
from auditor.tools import BrowserSession

console = Console()


def main() -> None:
    # Accept one or more YAML files: python run.py login.yaml requisition_claims.yaml
    yaml_paths = [Path(p) for p in sys.argv[1:]] if len(sys.argv) > 1 else [Path("claims.yaml")]
    config_path = Path("config.yaml")

    for p in yaml_paths:
        if not p.exists():
            console.print(f"[red]flows file not found: {p}[/red]")
            sys.exit(1)

    config = yaml.safe_load(config_path.read_text())
    flow_file = load_flows(*yaml_paths)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    output_dir = Path(config["evidence"]["output_dir"])

    total_claims = len(flow_file.all_claims)
    total_steps = len(flow_file.all_steps)
    console.print(f"\n[bold]Auditor MVP[/bold] — run [cyan]{run_id}[/cyan]")
    console.print(f"Flows: {len(flow_file.flows)}  Steps: {total_steps}  Claims: {total_claims}")
    console.print(f"Target: {config['app']['base_url']}\n")

    step_graph = build_step_graph(flow_file.flows)
    order = step_execution_order(step_graph)
    step_map = {s.id: s for s in flow_file.all_steps}

    llm = LLMClient(config["llm"])

    # Per-YAML fingerprint files: login.yaml → login.fingerprints.yaml
    per_yaml_stores: list[tuple[FingerprintStore, set[str]]] = []
    for yp in yaml_paths:
        fp_path = yp.with_name(f"{yp.stem}.fingerprints.yaml")
        store = FingerprintStore(fp_path)
        # Collect all claim IDs that belong to this YAML's flows
        from auditor.loader import load_flows as _lf
        yf = _lf(yp)
        claim_ids = {c.id for c in yf.all_claims}
        per_yaml_stores.append((store, claim_ids))
        console.print(f"  [dim]fingerprints: {fp_path.name} ({len(claim_ids)} claims)[/dim]")

    fp_router = FingerprintRouter(per_yaml_stores)
    all_evidence: list[dict] = []

    with BrowserSession(
        base_url=config["app"]["base_url"],
        headless=config["app"]["headless"],
        slow_mo_ms=config["app"].get("slow_mo_ms", 0),
    ) as session:
        session_data: dict[str, str] = {}

        for step_id in order:
            step = step_map[step_id]

            if step.status == StepStatus.blocked:
                console.print(f"  [yellow]⊘[/yellow] {step_id} — blocked (all claims skipped)")
                continue

            status, evidence_records, session_data = run_step(
                step=step,
                session=session,
                llm=llm,
                output_dir=output_dir,
                run_id=run_id,
                max_actions=config["agent"]["max_actions_per_claim"],
                session_data=session_data,
                fp_store=fp_router,
            )

            all_evidence.extend(evidence_records)

            icon = {"verified": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(str(status), "[yellow]⊘[/yellow]")
            console.print(f"  {icon} {step_id} — {status}\n")

            if status in (StepStatus.failed, StepStatus.blocked):
                blocked = cascade_step_failure(step_graph, step_id)
                mark_steps_blocked(flow_file.flows, set(blocked))
                if blocked:
                    console.print(f"  [dim]cascading block to steps: {', '.join(blocked)}[/dim]\n")

    report_path = Path("report.json")
    write_report(flow_file, all_evidence, run_id, report_path)
    print_summary(flow_file.all_claims, all_evidence, run_id)
    console.print(f"\n[dim]Report saved to {report_path}[/dim]")


if __name__ == "__main__":
    main()

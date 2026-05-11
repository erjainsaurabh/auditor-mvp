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
from auditor.loader import StepStatus, load_flows, load_test_data
from auditor.report import print_summary, write_report
from auditor.tools import BrowserSession


def _make_session(app_config: dict) -> BrowserSession:
    """Instantiate the right BrowserSession subclass based on config.platform."""
    platform = app_config.get("platform", "generic").lower()
    kwargs = dict(
        base_url=app_config["base_url"],
        headless=app_config["headless"],
        slow_mo_ms=app_config.get("slow_mo_ms", 0),
    )
    if platform == "ivalua":
        from auditor.platforms.ivalua import IvaluaBrowserSession
        return IvaluaBrowserSession(**kwargs)
    # Default: generic BrowserSession (no platform-specific strategies)
    return BrowserSession(**kwargs)

console = Console()


def main() -> None:
    # Accept one or more YAML files plus an optional --data flag:
    #   python run.py login.yaml requisition_claims.yaml
    #   python run.py login.yaml requisition_claims.yaml --data test_data.yaml
    args = sys.argv[1:]
    data_path: Path | None = None
    if "--data" in args:
        idx = args.index("--data")
        data_path = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
    yaml_paths = [Path(p) for p in args] if args else [Path("claims.yaml")]
    config_path = Path("config.yaml")

    # Fall back to test_data.yaml in the current directory if --data not given
    if data_path is None and Path("test_data.yaml").exists():
        data_path = Path("test_data.yaml")

    for p in yaml_paths:
        if not p.exists():
            console.print(f"[red]flows file not found: {p}[/red]")
            sys.exit(1)

    config = yaml.safe_load(config_path.read_text())
    flow_file = load_flows(*yaml_paths, test_data_path=data_path)
    if data_path:
        console.print(f"  test data: {data_path}")
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

    with _make_session(config["app"]) as session:
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

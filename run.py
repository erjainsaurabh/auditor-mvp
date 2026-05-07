from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

import yaml
from rich.console import Console

from auditor.agent import run_step
from auditor.graph import build_step_graph, cascade_step_failure, mark_steps_blocked, step_execution_order
from auditor.llm_client import LLMClient
from auditor.loader import StepStatus, load_flows
from auditor.report import print_summary, write_report
from auditor.tools import BrowserSession

console = Console()


def _login(session: BrowserSession, config: dict, console: Console) -> None:
    auth = config["app"].get("auth")
    if not auth:
        return
    username = os.environ.get("APP_USERNAME", "")
    password = os.environ.get("APP_PASSWORD", "")
    if not username or not password:
        console.print("[yellow]auth config present but APP_USERNAME/APP_PASSWORD not set in .env — skipping login[/yellow]")
        return

    console.print(f"  [dim]logging in as {username}…[/dim]")
    session.navigate(config["app"]["login_url"])
    session.fill_field(auth["username_field"], username)
    session.fill_field(auth["password_field"], password)
    if auth.get("submit", True):
        session.submit_form()
    try:
        session._page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    console.print("  [green]login complete[/green]\n")


def main() -> None:
    flows_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("claims.yaml")
    config_path = Path("config.yaml")

    if not flows_path.exists():
        console.print(f"[red]flows file not found: {flows_path}[/red]")
        sys.exit(1)

    config = yaml.safe_load(config_path.read_text())
    flow_file = load_flows(flows_path)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    output_dir = Path(config["evidence"]["output_dir"])

    total_claims = len(flow_file.all_claims)
    total_steps = len(flow_file.all_steps)
    console.print(f"\n[bold]Auditor MVP[/bold] — run [cyan]{run_id}[/cyan]")
    console.print(f"Flows: {len(flow_file.flows)}  Steps: {total_steps}  Claims: {total_claims}")
    console.print(f"Target: {config['app']['base_url']}\n")

    step_graph = build_step_graph(flow_file.flows)
    order = step_execution_order(step_graph)

    # Map step_id → step object across all flows
    step_map = {s.id: s for s in flow_file.all_steps}

    llm = LLMClient(config["llm"])
    all_evidence: list[dict] = []

    with BrowserSession(
        base_url=config["app"]["base_url"],
        headless=config["app"]["headless"],
        slow_mo_ms=config["app"].get("slow_mo_ms", 0),
    ) as session:
        _login(session, config, console)

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
    all_claims = flow_file.all_claims
    write_report(flow_file, all_evidence, run_id, report_path)
    print_summary(all_claims, all_evidence, run_id)
    console.print(f"\n[dim]Report saved to {report_path}[/dim]")


if __name__ == "__main__":
    main()

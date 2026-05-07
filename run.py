from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

import yaml
from rich.console import Console

from auditor.agent import run_claim
from auditor.evidence import EvidenceCollector
from auditor.graph import build_graph, cascade_failure, execution_order, mark_blocked
from auditor.llm_client import LLMClient
from auditor.loader import ClaimStatus, load_claims
from auditor.report import print_summary, write_report
from auditor.tools import BrowserSession

console = Console()


def _login(session: "BrowserSession", config: dict, console: "Console") -> None:
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
    claims_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("claims.yaml")
    config_path = Path("config.yaml")

    if not claims_path.exists():
        console.print(f"[red]claims file not found: {claims_path}[/red]")
        sys.exit(1)

    config = yaml.safe_load(config_path.read_text())
    claims_file = load_claims(claims_path)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    output_dir = Path(config["evidence"]["output_dir"])

    console.print(f"\n[bold]Auditor MVP[/bold] — run [cyan]{run_id}[/cyan]")
    console.print(f"Claims: {len(claims_file.claims)}  Target: {config['app']['base_url']}\n")

    graph = build_graph(claims_file.claims)
    order = execution_order(graph)
    claim_map = {c.id: c for c in claims_file.claims}

    llm = LLMClient(config["llm"])
    evidence_records: list[dict] = []

    with BrowserSession(
        base_url=config["app"]["base_url"],
        headless=config["app"]["headless"],
        slow_mo_ms=config["app"].get("slow_mo_ms", 0),
    ) as session:
        _login(session, config, console)

        prior_url: str | None = None

        for claim_id in order:
            claim = claim_map[claim_id]

            if claim.status == ClaimStatus.blocked:
                console.print(f"  [yellow]⊘[/yellow] {claim_id} — blocked")
                continue

            console.print(f"  [blue]~[/blue] {claim_id}: {claim.description}")
            ev = EvidenceCollector(run_id=run_id, claim_id=claim_id, output_dir=output_dir)

            status = run_claim(
                claim=claim,
                session=session,
                llm=llm,
                evidence=ev,
                max_actions=config["agent"]["max_actions_per_claim"],
                prior_url=prior_url,
            )

            prior_url = session.current_url()

            record = ev.finalize()
            evidence_records.append(record)

            icon = {"verified": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(status, "[yellow]⊘[/yellow]")
            console.print(f"  {icon} {claim_id} — {status}")

            if status in (ClaimStatus.failed, ClaimStatus.blocked):
                blocked = cascade_failure(graph, claim_id)
                mark_blocked(graph, blocked)
                if blocked:
                    console.print(f"    [dim]cascading block to: {', '.join(blocked)}[/dim]")

    report_path = Path("report.json")
    write_report(claims_file.claims, evidence_records, run_id, report_path)
    print_summary(claims_file.claims, evidence_records, run_id)
    console.print(f"\n[dim]Report saved to {report_path}[/dim]")


if __name__ == "__main__":
    main()

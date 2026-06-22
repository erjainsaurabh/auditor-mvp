from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from rich.console import Console

from flowprobe.logger import get_logger, setup_logging, setup_logging_from_config
# Bootstrap with file logging immediately; Seq is wired in setup_logging_from_config.
from flowprobe.config import settings
setup_logging(log_file=Path(__file__).parent / settings.log_file)
log = get_logger("run")

from flowprobe.agent import run_test_condition
from flowprobe.fingerprint import FingerprintRouter, FingerprintStore
from flowprobe.graph import build_condition_graph, cascade_condition_failure, condition_execution_order, mark_conditions_blocked
from flowprobe.llm_client import LLMClient
from flowprobe.loader import ConditionStatus, load_flows
from flowprobe.report import print_summary, write_report
from flowprobe.pattern_inventory import PatternInventory
from flowprobe.strategy_stats import StrategyStats
from flowprobe.tools import BrowserSession


def _make_session(base_url: str, run_id: str = "") -> BrowserSession:
    kwargs = dict(
        base_url=base_url,
        headless=settings.flowprobe_headless,
        slow_mo_ms=settings.slow_mo_ms,
        run_id=run_id,
    )
    if settings.platform == "ivalua":
        from flowprobe.platforms.ivalua import IvaluaBrowserSession
        return IvaluaBrowserSession(**kwargs)
    return BrowserSession(**kwargs)


console = Console()


def run_audit(
    yaml_paths: list[Path],
    data_path: Path | None = None,
    report_path: Path = Path("report.json"),
    run_id: str | None = None,
) -> dict:
    """Execute an audit run and return the report as a dict.

    Writes the report to *report_path* and returns the same data so callers
    (CLI, API) can use it without re-reading the file.
    """
    log.info("run_audit starting — yamls=%s data=%s", yaml_paths, data_path)
    setup_logging_from_config({})

    flow_file = load_flows(*yaml_paths, test_data_path=data_path)
    run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
    output_dir = Path(settings.evidence_output_dir)

    # Flow YAML config section can override base_url — this is how
    # the TestManagement app injects the environment base_url into each run.
    base_url = settings.base_url
    flow_base_url = flow_file.config.get("base_url") if flow_file.config else None
    if flow_base_url:
        log.info("base_url overridden by flow YAML: %s", flow_base_url)
        base_url = flow_base_url

    log.info(
        "run_id=%s  flows=%d  test_conditions=%d  steps=%d",
        run_id,
        len(flow_file.flows),
        len(flow_file.all_test_conditions),
        len(flow_file.all_steps),
    )

    total_steps = len(flow_file.all_steps)
    total_tcs = len(flow_file.all_test_conditions)
    console.print(f"\n[bold]FlowProbe[/bold] — run [cyan]{run_id}[/cyan]")
    console.print(f"Flows: {len(flow_file.flows)}  Test Conditions: {total_tcs}  Steps: {total_steps}")
    console.print(f"Target: {base_url}\n")
    if data_path:
        console.print(f"  test data: {data_path}")

    condition_graph = build_condition_graph(flow_file.flows)
    order = condition_execution_order(condition_graph)
    tc_map = {tc.id: tc for tc in flow_file.all_test_conditions}

    if settings.platform == "ivalua":
        from flowprobe.platforms.ivalua import IvaluaBrowserSession
        platform_guidance = IvaluaBrowserSession.PLATFORM_GUIDANCE
    else:
        platform_guidance = ""
    llm = LLMClient(platform_guidance=platform_guidance)

    fingerprints_dir = Path(settings.fingerprints_dir)
    fingerprints_dir.mkdir(parents=True, exist_ok=True)

    per_yaml_stores: list[tuple[FingerprintStore, set[str]]] = []
    for yp in yaml_paths:
        fp_path = fingerprints_dir / f"{yp.stem}.fingerprints.yaml"
        store = FingerprintStore(fp_path, source_file=yp.stem)
        from flowprobe.loader import load_flows as _lf
        yf = _lf(yp)
        step_ids = {s.id for s in yf.all_steps}
        per_yaml_stores.append((store, step_ids))
        console.print(f"  [dim]fingerprints: {fp_path} ({len(step_ids)} steps)[/dim]")

    fp_router = FingerprintRouter(per_yaml_stores)

    stats_path = fingerprints_dir / "strategy_stats.yaml"
    stats = StrategyStats(stats_path, platform=settings.platform)
    console.print(f"  [dim]strategy stats: {stats_path} (platform={settings.platform})[/dim]")

    inventory_path = fingerprints_dir / "pattern_inventory.yaml"
    inventory = PatternInventory(inventory_path, platform=settings.platform)
    console.print(f"  [dim]pattern inventory: {inventory_path} (platform={settings.platform})[/dim]")

    all_evidence: list[dict] = []

    with _make_session(base_url, run_id=run_id) as session:
        session._stats = stats
        session_data: dict[str, str] = {}

        for tc_id in order:
            tc = tc_map[tc_id]

            if tc.status == ConditionStatus.blocked:
                log.info("tc %s — blocked, skipping", tc_id)
                console.print(f"  [yellow]⊘[/yellow] {tc_id} — blocked (all steps skipped)")
                continue

            log.info("tc %s — starting", tc_id)
            status, evidence_records, session_data = run_test_condition(
                tc=tc,
                session=session,
                llm=llm,
                output_dir=output_dir,
                run_id=run_id,
                max_actions=settings.max_actions_per_claim,
                session_data=session_data,
                fp_store=fp_router,
                pattern_inventory=inventory,
            )

            all_evidence.extend(evidence_records)

            log.info("tc %s — completed: status=%s", tc_id, status.value)
            icon = {"verified": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(status.value, "[yellow]⊘[/yellow]")
            console.print(f"  {icon} {tc_id} — {status.value}\n")

            if status in (ConditionStatus.failed, ConditionStatus.blocked):
                blocked = cascade_condition_failure(condition_graph, tc_id)
                mark_conditions_blocked(flow_file.flows, set(blocked))
                if blocked:
                    console.print(f"  [dim]cascading block to test conditions: {', '.join(blocked)}[/dim]\n")

    # Persist stats — force=True ensures the file is created even when all
    # steps used fingerprint replay (no live strategy calls were made).
    stats.save(force=True)
    console.print(f"  [dim]strategy stats saved → {stats_path}[/dim]")
    log.info("strategy stats saved to %s", stats_path)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("writing report to %s", report_path)
    try:
        write_report(flow_file, all_evidence, run_id, report_path)
    except Exception:
        import traceback
        log.error("write_report failed:\n%s", traceback.format_exc())
        raise
    print_summary(flow_file.all_steps, all_evidence, run_id)
    console.print(f"\n[dim]Report saved to {report_path}[/dim]")
    log.info("run_audit complete — report at %s", report_path)

    return json.loads(report_path.read_text())


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

    if data_path is None and Path("test_data.yaml").exists():
        data_path = Path("test_data.yaml")

    for p in yaml_paths:
        if not p.exists():
            console.print(f"[red]flows file not found: {p}[/red]")
            sys.exit(1)

    run_audit(yaml_paths, data_path)


if __name__ == "__main__":
    main()

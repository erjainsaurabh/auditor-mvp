"""FastAPI wrapper for flowprobe.

Endpoints:
    POST /run                     — start an audit run (returns run_id immediately)
    GET  /run/{run_id}/status     — poll for queued | running | done | failed
                                    result field: passed | failed | partial | null
    GET  /run/{run_id}/report     — fetch the report (404 while running, 200 when done)

Usage:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import json
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ── logging bootstrap (must happen before any flowprobe import) ─────────────────
from flowprobe.logger import get_logger, setup_logging, setup_logging_from_config
from flowprobe.config import settings
setup_logging(log_file=Path(__file__).parent / settings.log_file)
try:
    setup_logging_from_config({})
except Exception:
    pass  # Seq failure must never crash the API
log = get_logger("api")

from run import run_audit
from flowprobe.storage.object_store import upload_run_downloads

# ---------------------------------------------------------------------------
# Job store — in-memory, sufficient for single-instance deployments
# ---------------------------------------------------------------------------

@dataclass
class _Job:
    run_id: str
    status: str = "queued"        # queued | running | done | failed
    result: str | None = None     # passed | failed | partial  (set when status=done)
    summary: dict | None = None   # snapshot of report summary counts
    report: dict[str, Any] | None = None
    error: str | None = None


_jobs: dict[str, _Job] = {}
_jobs_lock = Lock()

# One browser = one Playwright context at a time; set max_workers=1 to avoid
# concurrent Playwright sessions on the same machine. Raise to 2+ only if you
# run headless on a machine with enough RAM for multiple Chrome instances.
_executor = ThreadPoolExecutor(max_workers=1)


def _recover_jobs() -> None:
    """On startup, scan evidence/<run_id>/report.json files and rebuild _jobs.

    This lets the status/report endpoints return correct data after a process
    restart (e.g. deploy) for any run that already wrote its report to disk.
    Runs that were still in-progress when the process died will remain missing —
    those are genuinely unrecoverable without a persistent queue.
    """
    import json
    evidence_dir = Path(__file__).parent / "evidence"
    if not evidence_dir.exists():
        return
    recovered = 0
    for report_path in sorted(evidence_dir.glob("*/report.json")):
        run_id = report_path.parent.name
        if not run_id.startswith("run_") or run_id in _jobs:
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            summary = report.get("summary", {})
            failed   = summary.get("failed", 0)
            blocked  = summary.get("blocked", 0)
            verified = summary.get("verified", 0)
            result = (
                "passed"  if failed == 0 and blocked == 0 else
                "failed"  if verified == 0 else
                "partial"
            )
            job = _Job(run_id=run_id, status="done",
                       result=result, summary=summary, report=report)
            _jobs[run_id] = job
            recovered += 1
        except Exception:
            pass  # corrupt / incomplete report — skip
    if recovered:
        log.info("startup recovery: restored %d completed runs from evidence/", recovered)


_recover_jobs()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="FlowProbe API", version="1.0.0")


class RunRequest(BaseModel):
    # ── File-path mode (local dev, shared filesystem) ────────────────────────
    yamls: list[str] = []
    data: str | None = None
    # ── Content mode (distributed / Fargate — no shared filesystem needed) ───
    # When yaml_contents is provided the server writes them to a per-run temp
    # directory and uses those paths; file-path fields are ignored.
    yaml_contents: list[str] = []          # YAML text for each flow file
    yaml_filenames: list[str] = []         # Logical filenames (e.g. "plan_42.yaml")
    data_content: str | None = None        # test_data.yaml text


class RunResponse(BaseModel):
    run_id: str


class StatusResponse(BaseModel):
    run_id: str
    status: str                   # queued | running | done | failed
    result: str | None = None     # passed | failed | partial  (null until done)
    summary: dict | None = None   # total/verified/failed/blocked counts
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report_has_artifacts(report: dict) -> bool:
    """Return True if any step in the report has a non-null artifact block."""
    for flow in report.get("flows", []):
        for tc in flow.get("test_conditions", []):
            for step in tc.get("steps", []):
                if step.get("artifact") is not None:
                    return True
    return False


def _inject_artifact_urls(report: dict, url_map: dict[str, str]) -> None:
    """Walk the report and stamp each step's artifact block with its presigned URL.

    evidence.artifact is written by EvidenceCollector.finalize() and already
    contains {type, filename} from the step's produces.type declaration.
    This function adds the url field once object storage upload completes.
    """
    for flow in report.get("flows", []):
        for tc in flow.get("test_conditions", []):
            for step in tc.get("steps", []):
                artifact = step.get("artifact")
                if artifact and artifact.get("filename") in url_map:
                    artifact["url"] = url_map[artifact["filename"]]
                    # also patch inside evidence block so evidence.json stays consistent
                    (step.get("evidence") or {}).get("artifact") and step["evidence"]["artifact"].update({"url": artifact["url"]})


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _execute(job: _Job, yaml_paths: list[Path], data_path: Path | None) -> None:
    log.info("run %s — starting (yamls=%s)", job.run_id, [p.name for p in yaml_paths])
    with _jobs_lock:
        job.status = "running"
    try:
        # Each run writes its report under evidence/<run_id>/report.json so
        # concurrent runs (if max_workers>1) never overwrite each other.
        report_path = Path("evidence") / job.run_id / "report.json"
        log.debug("run %s — report_path=%s", job.run_id, report_path)
        report = run_audit(
            yaml_paths=yaml_paths,
            data_path=data_path,
            report_path=report_path,
            run_id=job.run_id,
        )
        summary = report.get("summary", {})
        failed = summary.get("failed", 0)
        blocked = summary.get("blocked", 0)
        verified = summary.get("verified", 0)
        total = summary.get("total", 0)
        if failed == 0 and blocked == 0:
            result = "passed"
        elif verified == 0:
            result = "failed"
        else:
            result = "partial"
        log.info(
            "run %s — done: result=%s total=%d verified=%d failed=%d blocked=%d",
            job.run_id, result, total, verified, failed, blocked,
        )
        # Only upload to object storage when at least one step produced an artifact
        # (i.e. had produces.type declared in YAML and a download succeeded).
        if _report_has_artifacts(report):
            log.info("run %s — artifacts found, uploading to object storage", job.run_id)
            try:
                url_map = upload_run_downloads(job.run_id)
                if url_map:
                    _inject_artifact_urls(report, url_map)
                    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    log.info("run %s — injected presigned URLs for: %s", job.run_id, list(url_map.keys()))
            except Exception as upload_exc:
                log.warning("run %s — object storage unavailable, skipping upload: %s", job.run_id, upload_exc)
        else:
            log.info("run %s — no artifacts declared, skipping object storage upload", job.run_id)

        with _jobs_lock:
            job.report = report
            job.summary = summary
            job.result = result
            job.status = "done"
    except Exception as exc:
        tb = traceback.format_exc()
        # Log the FULL traceback so it appears in flowprobe.log and stderr
        log.error(
            "run %s — FAILED with %s: %s\n%s",
            job.run_id, type(exc).__name__, exc, tb,
        )
        with _jobs_lock:
            # Store both the short message and full traceback for the API response
            job.error = f"{type(exc).__name__}: {exc}\n\nTraceback:\n{tb}"
            job.status = "failed"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/run", response_model=RunResponse, status_code=202)
def start_run(req: RunRequest) -> RunResponse:
    """Queue an audit run and return a run_id immediately.

    Supports two modes:
    • File-path mode (local dev):   set ``yamls`` / ``data`` to paths relative
      to the flowprobe root directory.
    • Content mode (distributed):   set ``yaml_contents`` / ``yaml_filenames`` /
      ``data_content`` with the raw YAML text.  The server writes them to a
      per-run staging directory so the rest of the pipeline is unchanged.
    """
    base = Path(__file__).parent
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # ── Content mode ─────────────────────────────────────────────────────────
    if req.yaml_contents:
        if len(req.yaml_contents) != len(req.yaml_filenames):
            raise HTTPException(
                status_code=400,
                detail="yaml_contents and yaml_filenames must have the same length",
            )
        staging_dir = base / "evidence" / run_id / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        yaml_paths: list[Path] = []
        for filename, content in zip(req.yaml_filenames, req.yaml_contents):
            p = staging_dir / filename
            p.write_text(content, encoding="utf-8")
            yaml_paths.append(p)
            log.info("POST /run — wrote staging YAML: %s (%d chars)", p.name, len(content))

        data_path: Path | None = None
        if req.data_content:
            data_path = staging_dir / "test_data.yaml"
            data_path.write_text(req.data_content, encoding="utf-8")
            log.info("POST /run — wrote staging test_data.yaml (%d chars)", len(req.data_content))
        elif (base / "test_data.yaml").exists():
            data_path = base / "test_data.yaml"

    # ── File-path mode ────────────────────────────────────────────────────────
    else:
        if not req.yamls:
            raise HTTPException(status_code=400, detail="Provide either yamls (file paths) or yaml_contents")
        yaml_paths = [base / y for y in req.yamls]
        for p in yaml_paths:
            if not p.exists():
                log.warning("POST /run — YAML not found: %s", p)
                raise HTTPException(status_code=400, detail=f"YAML not found: {p.name}")

        data_path = None
        if req.data:
            data_path = base / req.data
            if not data_path.exists():
                log.warning("POST /run — data file not found: %s", req.data)
                raise HTTPException(status_code=400, detail=f"Data file not found: {req.data}")
        elif (base / "test_data.yaml").exists():
            data_path = base / "test_data.yaml"

    job = _Job(run_id=run_id)
    log.info("POST /run — queued %s (yamls=%s, data=%s)", run_id, [p.name for p in yaml_paths], data_path)

    with _jobs_lock:
        _jobs[run_id] = job

    _executor.submit(_execute, job, yaml_paths, data_path)
    return RunResponse(run_id=run_id)


@app.get("/run/{run_id}/status", response_model=StatusResponse)
def get_status(run_id: str) -> StatusResponse:
    """Return the current status of a run."""
    job = _jobs.get(run_id)
    if job is None:
        log.debug("GET /status %s — not found", run_id)
        raise HTTPException(status_code=404, detail="run_id not found")
    log.debug("GET /status %s — status=%s result=%s", run_id, job.status, job.result)
    return StatusResponse(
        run_id=run_id,
        status=job.status,
        result=job.result,
        summary=job.summary,
        error=job.error,
    )


@app.get("/run/{run_id}/report")
def get_report(run_id: str) -> dict:  # noqa: F811
    """Return the full report once the run is done.

    Returns 404 if the run_id is unknown, 202 if still running/queued,
    500 if the run failed, and 200 with the report body when done.
    """
    job = _jobs.get(run_id)
    if job is None:
        log.debug("GET /report %s — not found", run_id)
        raise HTTPException(status_code=404, detail="run_id not found")
    if job.status in ("queued", "running"):
        log.debug("GET /report %s — still %s", run_id, job.status)
        raise HTTPException(status_code=202, detail=f"Run is {job.status}")
    if job.status == "failed":
        log.error("GET /report %s — run failed, returning 500. error=%s", run_id, job.error)
        raise HTTPException(status_code=500, detail=job.error or "run failed")
    log.info("GET /report %s — returning report (%d flows)", run_id, len((job.report or {}).get("flows", [])))
    return job.report


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)

"""FastAPI wrapper for auditor-mvp.

Endpoints:
    POST /run                     — start an audit run (returns run_id immediately)
    GET  /run/{run_id}/status     — poll for queued | running | done | failed
                                    result field: passed | failed | partial | null
    GET  /run/{run_id}/report     — fetch the report (404 while running, 200 when done)

Usage:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from run import run_audit

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

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Auditor MVP API", version="1.0.0")


class RunRequest(BaseModel):
    yamls: list[str]
    data: str | None = None


class RunResponse(BaseModel):
    run_id: str


class StatusResponse(BaseModel):
    run_id: str
    status: str                   # queued | running | done | failed
    result: str | None = None     # passed | failed | partial  (null until done)
    summary: dict | None = None   # total/verified/failed/blocked counts
    error: str | None = None


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _execute(job: _Job, yaml_paths: list[Path], data_path: Path | None) -> None:
    with _jobs_lock:
        job.status = "running"
    try:
        # Each run writes its report under evidence/<run_id>/report.json so
        # concurrent runs (if max_workers>1) never overwrite each other.
        report_path = Path("evidence") / job.run_id / "report.json"
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
        with _jobs_lock:
            job.report = report
            job.summary = summary
            job.result = result
            job.status = "done"
    except Exception as exc:
        with _jobs_lock:
            job.error = str(exc)
            job.status = "failed"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/run", response_model=RunResponse, status_code=202)
def start_run(req: RunRequest) -> RunResponse:
    """Queue an audit run and return a run_id immediately."""
    base = Path(__file__).parent
    yaml_paths = [base / y for y in req.yamls]
    for p in yaml_paths:
        if not p.exists():
            raise HTTPException(status_code=400, detail=f"YAML not found: {p.name}")

    data_path: Path | None = None
    if req.data:
        data_path = base / req.data
        if not data_path.exists():
            raise HTTPException(status_code=400, detail=f"Data file not found: {req.data}")
    elif (base / "test_data.yaml").exists():
        data_path = base / "test_data.yaml"

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    job = _Job(run_id=run_id)

    with _jobs_lock:
        _jobs[run_id] = job

    _executor.submit(_execute, job, yaml_paths, data_path)
    return RunResponse(run_id=run_id)


@app.get("/run/{run_id}/status", response_model=StatusResponse)
def get_status(run_id: str) -> StatusResponse:
    """Return the current status of a run."""
    job = _jobs.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="run_id not found")
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
        raise HTTPException(status_code=404, detail="run_id not found")
    if job.status in ("queued", "running"):
        raise HTTPException(status_code=202, detail=f"Run is {job.status}")
    if job.status == "failed":
        raise HTTPException(status_code=500, detail=job.error or "run failed")
    return job.report


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)

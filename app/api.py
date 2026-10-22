"""
FastAPI service exposing the measurement-run catalog.

Run with: uvicorn app.api:app --reload
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import catalog

app = FastAPI(
    title="Quantum Measurement Catalog",
    description=(
        "Catalogs legacy quantum-hardware measurement runs built on real "
        "IBM Quantum device calibration data, computes summary statistics, "
        "and flags unreadable files for archiving decisions."
    ),
    version="0.1.0",
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ScanResponse(BaseModel):
    files_scanned: int
    runs_cataloged: int
    corrupt_files: int
    duration_seconds: float
    peak_process_memory_mb: float
    parser_object_memory_kb: float


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/scan", response_model=ScanResponse)
def trigger_scan() -> catalog.ScanReport:
    """Re-scan the data directory and rebuild the catalog. Parallelized
    with a thread pool, cached per-file with DiskCache, and reports
    resource usage via psutil and Pympler."""
    return catalog.scan()


@app.get("/runs")
def list_runs(
    device_name: Optional[str] = Query(default=None),
    circuit_type: Optional[str] = Query(default=None, pattern="^(bell_pair|ry_sweep)$"),
    limit: int = Query(default=100, le=500),
) -> list[dict]:
    return catalog.query_runs(device_name=device_name, circuit_type=circuit_type, limit=limit)


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = catalog.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    # Attach an exact binomial confidence interval for this one run,
    # computed on demand (expensive to do for every row in a bulk scan).
    successes = int(round(run["fidelity_estimate"] * run["shots"])) if run["shots"] else 0
    low, high = catalog.binomial_confidence_interval(successes, run["shots"])
    run["fidelity_ci_95"] = {"low": low, "high": high}
    return run


@app.get("/devices/summary")
def devices_summary() -> list[dict]:
    return catalog.device_summary()


@app.get("/corrupt-files")
def list_corrupt_files() -> list[dict]:
    """Files flagged during the last scan as unreadable, candidates for
    archiving rather than continued attempts to parse."""
    return catalog.corrupt_files()

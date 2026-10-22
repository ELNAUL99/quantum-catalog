"""
Catalog layer: scans the legacy JSON run files, tolerates schema drift
across eras, computes summary stats with numpy/scipy, caches parsed
results with DiskCache, and persists the catalog in SQLite.

Resource usage during a scan is tracked with psutil (process-level
CPU/memory) and Pympler (object-level memory of the heaviest step),
and the per-file parse work is parallelized with concurrent.futures.
"""
from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import psutil
from diskcache import Cache
from pympler import asizeof
from scipy import stats as sp_stats

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "runs"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "catalog.db"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

_cache = Cache(str(CACHE_DIR))


class CorruptRunFile(Exception):
    pass


@dataclass
class ParsedRun:
    run_id: str
    timestamp: Optional[str]
    device_name: Optional[str]
    device_num_qubits: Optional[int]
    circuit_type: Optional[str]
    shots: int
    counts: dict
    control_theta: Optional[float]
    readout_error: Optional[float]
    t1_us: Optional[float]
    t2_us: Optional[float]


def _normalize(raw: dict, fallback_id: str) -> ParsedRun:
    """Normalize a run file into one shape for the catalog. Real devices
    differ in what calibration fields they report (an older 1-qubit
    device has far less than a 127-qubit Eagle-era chip), so the
    'archaeology' here is genuinely about which fields exist for a
    given device rather than an invented format change."""
    calibration = raw.get("calibration") or {}
    qubits_used = raw.get("qubits_used") or []
    first_qubit_cal = calibration.get(str(qubits_used[0])) if qubits_used else None
    if first_qubit_cal is None and qubits_used:
        # JSON object keys are strings; calibration may also be keyed by int
        # if loaded before a round trip through json.dumps/loads.
        first_qubit_cal = calibration.get(qubits_used[0], {})
    first_qubit_cal = first_qubit_cal or {}

    return ParsedRun(
        run_id=raw.get("run_id", fallback_id),
        timestamp=raw.get("timestamp"),
        device_name=raw.get("device_name"),
        device_num_qubits=raw.get("device_num_qubits"),
        circuit_type=raw.get("circuit_type"),
        shots=raw.get("shots", sum(raw.get("counts", {}).values())),
        counts=raw.get("counts", {}),
        control_theta=raw.get("control_theta"),
        readout_error=first_qubit_cal.get("readout_error"),
        t1_us=first_qubit_cal.get("t1_us"),
        t2_us=first_qubit_cal.get("t2_us"),
    )


def _fidelity_estimate(counts: dict, shots: int) -> float:
    """Treat the dominant outcome's share of shots as a rough fidelity
    proxy, the same kind of quick-look metric a calibration dashboard
    would compute per run."""
    if not counts or shots == 0:
        return 0.0
    top = max(counts.values())
    return round(top / shots, 4)


def _error_bar(counts: dict, shots: int) -> float:
    """Standard normal-approximation error on the dominant outcome's
    probability, a quick-look uncertainty estimate for the fidelity
    proxy above. scipy.stats backs the exact binomial check in the
    test suite; this is the cheap version used on every row of a scan."""
    if shots == 0 or not counts:
        return 0.0
    p = max(counts.values()) / shots
    return round(float(np.sqrt(p * (1 - p) / shots)), 5)


def binomial_confidence_interval(successes: int, shots: int, confidence: float = 0.95) -> tuple[float, float]:
    """Exact binomial confidence interval via scipy.stats, used by the
    API's per-run detail endpoint rather than the bulk scan (it is
    noticeably more expensive than the normal approximation above)."""
    if shots == 0:
        return (0.0, 0.0)
    result = sp_stats.binomtest(successes, shots)
    ci = result.proportion_ci(confidence_level=confidence)
    return (round(ci.low, 5), round(ci.high, 5))


def parse_file(path: Path) -> ParsedRun:
    """Parse one run file, using DiskCache keyed on path + mtime so an
    unchanged file is never re-parsed on a repeat scan."""
    stat = path.stat()
    cache_key = f"{path.name}:{stat.st_mtime_ns}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CorruptRunFile(f"{path.name}: {exc}") from exc

    parsed = _normalize(raw, fallback_id=path.stem)
    _cache.set(cache_key, parsed)
    return parsed


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            timestamp TEXT,
            device_name TEXT,
            device_num_qubits INTEGER,
            circuit_type TEXT,
            shots INTEGER,
            fidelity_estimate REAL,
            error_bar REAL,
            control_theta REAL,
            readout_error REAL,
            t1_us REAL,
            t2_us REAL,
            source_file TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corrupt_files (
            source_file TEXT PRIMARY KEY,
            error TEXT,
            flagged_for_archive INTEGER DEFAULT 1
        )
        """
    )
    conn.commit()


@dataclass
class ScanReport:
    files_scanned: int
    runs_cataloged: int
    corrupt_files: int
    duration_seconds: float
    peak_process_memory_mb: float
    parser_object_memory_kb: float


def scan(data_dir: Optional[Path] = None, db_path: Optional[Path] = None, max_workers: int = 4) -> ScanReport:
    # Resolved at call time rather than bound as default-argument values,
    # so tests (and callers in general) can point this at a different
    # data/catalog location by reassigning the module-level constants.
    data_dir = data_dir if data_dir is not None else DATA_DIR
    db_path = db_path if db_path is not None else DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(data_dir.glob("*.json"))

    process = psutil.Process()
    start_mem = process.memory_info().rss
    start = time.monotonic()

    parsed_runs: list[ParsedRun] = []
    corrupt: list[tuple[str, str]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_path = {pool.submit(parse_file, p): p for p in files}
        for future in concurrent.futures.as_completed(future_to_path):
            path = future_to_path[future]
            try:
                parsed_runs.append(future.result())
            except CorruptRunFile as exc:
                corrupt.append((path.name, str(exc)))

    # Pympler: measure the in-memory footprint of the heaviest structure
    # we build during a scan, the full list of parsed runs.
    parser_object_kb = asizeof.asizeof(parsed_runs) / 1024

    conn = sqlite3.connect(db_path)
    _init_db(conn)
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM corrupt_files")

    for run in parsed_runs:
        fidelity = _fidelity_estimate(run.counts, run.shots)
        err = _error_bar(run.counts, run.shots)
        conn.execute(
            """INSERT OR REPLACE INTO runs
               (run_id, timestamp, device_name, device_num_qubits, circuit_type,
                shots, fidelity_estimate, error_bar, control_theta, readout_error,
                t1_us, t2_us, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.run_id, run.timestamp, run.device_name, run.device_num_qubits,
                run.circuit_type, run.shots, fidelity, err, run.control_theta,
                run.readout_error, run.t1_us, run.t2_us, run.run_id + ".json",
            ),
        )
    for fname, err in corrupt:
        conn.execute(
            "INSERT OR REPLACE INTO corrupt_files (source_file, error) VALUES (?, ?)",
            (fname, err),
        )
    conn.commit()
    conn.close()

    duration = time.monotonic() - start
    peak_mem_mb = (process.memory_info().rss - start_mem) / (1024 * 1024)

    return ScanReport(
        files_scanned=len(files),
        runs_cataloged=len(parsed_runs),
        corrupt_files=len(corrupt),
        duration_seconds=round(duration, 4),
        peak_process_memory_mb=round(max(peak_mem_mb, 0.0), 3),
        parser_object_memory_kb=round(parser_object_kb, 2),
    )


def query_runs(db_path: Optional[Path] = None, device_name: Optional[str] = None,
               circuit_type: Optional[str] = None, limit: int = 100) -> list[dict]:
    db_path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM runs WHERE 1=1"
    params: list = []
    if device_name is not None:
        sql += " AND device_name = ?"
        params.append(device_name)
    if circuit_type is not None:
        sql += " AND circuit_type = ?"
        params.append(circuit_type)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def device_summary(db_path: Optional[Path] = None) -> list[dict]:
    """Per-device rollup: how many runs, average fidelity proxy, and the
    real T1 range seen for that device, the kind of query a calibration
    drift dashboard would run first."""
    db_path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT device_name,
               COUNT(*) AS run_count,
               ROUND(AVG(fidelity_estimate), 4) AS avg_fidelity,
               ROUND(MIN(t1_us), 2) AS min_t1_us,
               ROUND(MAX(t1_us), 2) AS max_t1_us
        FROM runs
        GROUP BY device_name
        ORDER BY run_count DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run(run_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    db_path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def corrupt_files(db_path: Optional[Path] = None) -> list[dict]:
    db_path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM corrupt_files").fetchall()
    conn.close()
    return [dict(r) for r in rows]

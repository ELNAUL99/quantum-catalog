# Quantum Measurement Catalog

A legacy-data archaeology and cataloging tool for quantum-hardware measurement runs, built on **real IBM Quantum device calibration data**.

## Why this exists

I built this to work directly with the kind of problem a quantum-hardware R&D team actually has: years of measurement files accumulated across different device generations, with inconsistent metadata, some of it corrupted, that needs to be cataloged, analyzed, and triaged for archiving.

Qiskit ships calibration snapshots ("fake backends") taken from real IBM Quantum processors at points in their operating history, real per-qubit T1/T2 times, gate errors, and readout errors, spanning hardware generations from 1-5 qubit early devices up to 127-qubit Eagle-generation chips (`fake_sherbrooke`, `fake_washington`). This project runs real circuits (Bell pairs, Ry calibration sweeps) through each real device's actual noise model via Qiskit Aer, so the measurement counts carry genuine decoherence and readout error, not invented numbers. The resulting measurement runs are then treated exactly like a legacy lab archive: cataloged, analyzed, and some deliberately corrupted to simulate real-world file damage.

## What it does

- **Data generation** (`app/generate_data.py`): runs real circuits against real IBM device noise models (Qiskit, Qiskit Aer, `qiskit-ibm-runtime` fake providers) and writes each result as a JSON measurement file, the way an internal lab logging system would.
- **Cataloging** (`app/catalog.py`): parses the JSON archive, tolerant of the genuine differences in what each real device generation's calibration data reports, computes summary statistics with `numpy` and `scipy.stats` (a fidelity proxy, its standard error, and an exact binomial confidence interval), and persists the catalog in **SQLite**.
  - Parsing is parallelized with `concurrent.futures`.
  - Parsed files are cached with `diskcache`, keyed on path and modification time, so an unchanged file is never re-parsed on a repeat scan.
  - A scan reports its own resource usage via `psutil` (process memory/CPU) and `pympler` (in-memory size of the parsed-run collection).
  - Malformed files are caught and flagged in a separate `corrupt_files` table rather than crashing the scan, as candidates for archiving decisions.
- **API** (`app/api.py`): a FastAPI service (run with Uvicorn) exposing the catalog: trigger a re-scan, list and filter runs, fetch one run's detail with a computed confidence interval, get a per-device summary, and list files flagged as corrupt.
- **Dashboard** (`static/index.html`): a small page hitting the API directly, showing per-device summary stats, a filterable run table, and the corrupt-file list, the kind of lightweight internal tool a small team builds for itself rather than waiting on a heavier BI platform.
- **Tests** (`tests/`): `pytest` covering the schema-tolerant parser (full calibration vs. missing fields), corrupt-file handling, caching behavior, the statistics functions, a full scan end to end, and the API's happy and error paths. `flake8`-clean.

## Running it

```bash
pip install -r requirements.txt

# Generate a batch of real-device-noise measurement runs
python -m app.generate_data

# Run the test suite and linter
pytest
flake8 app tests

# Start the API + dashboard
uvicorn app.api:app --reload
# then open http://127.0.0.1:8000
```

## Notes

- All measurement counts come from real circuit execution against real device noise models, not fabricated numbers. The devices themselves are historical calibration snapshots shipped with Qiskit (not live hardware access), so this is a self-built foundation project, not production work on a live measurement system.
- A small fraction of files are deliberately truncated during generation to simulate real-world file corruption and exercise the archive-triage path.

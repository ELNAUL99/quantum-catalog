# Quantum Measurement Catalog

Legacy-data cataloging tool for quantum-hardware measurement runs, built on real IBM
Quantum device calibration data (Qiskit "fake backends" + Aer noise models). FastAPI +
SQLite service with a schema-tolerant parser, caching, summary statistics, and a small
dashboard. See `README.md` for the full narrative.

## Commands

```bash
pip install -r requirements.txt   # install deps
python -m app.generate_data       # generate real-device-noise measurement runs into data/runs/
pytest                            # run the test suite
flake8 app tests                  # lint (config in setup.cfg, max-line-length 110)
uvicorn app.api:app --reload      # start API + dashboard at http://127.0.0.1:8000
```

## Layout

- `app/generate_data.py` — runs real circuits through real IBM device noise models, writes JSON run files.
- `app/catalog.py` — schema-tolerant parser, numpy/scipy stats, DiskCache, SQLite persistence (the core).
- `app/api.py` — FastAPI endpoints: `/scan`, `/runs`, `/runs/{id}`, `/devices/summary`, `/corrupt-files`.
- `static/index.html` — dashboard hitting the API directly.
- `tests/` — pytest covering the parser, corrupt-file handling, caching, stats, a full scan, and API paths.

## Notes

- Generated artifacts live under `data/` (runs, cache, catalog.db) and are gitignored — regenerate with `python -m app.generate_data`.
- A small fraction of generated files are deliberately truncated to exercise the corrupt-file / archive-triage path; this is intentional, not a bug.
- Device calibration schemas genuinely differ across hardware generations (1-qubit → 127-qubit Eagle); the parser's tolerance of missing fields is by design, not defensive guesswork.

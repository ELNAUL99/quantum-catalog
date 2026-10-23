import json

import pytest

from app import catalog


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _write(path, payload):
    path.write_text(json.dumps(payload))


def test_normalize_handles_full_calibration(run_dir):
    payload = {
        "run_id": "run_00001",
        "timestamp": "2024-01-01T00:00:00",
        "device_name": "fake_sherbrooke",
        "device_num_qubits": 127,
        "qubits_used": [0, 1],
        "circuit_type": "bell_pair",
        "control_theta": None,
        "shots": 512,
        "counts": {"00": 250, "11": 240, "01": 12, "10": 10},
        "calibration": {"0": {"t1_us": 380.1, "t2_us": 130.2, "readout_error": 0.012}},
    }
    parsed = catalog._normalize(payload, fallback_id="x")
    assert parsed.device_name == "fake_sherbrooke"
    assert parsed.t1_us == 380.1
    assert parsed.readout_error == 0.012


def test_normalize_handles_missing_calibration(run_dir):
    # Older/smaller real devices report fewer calibration fields; the
    # parser must not fail, it should leave those fields as None.
    payload = {
        "run_id": "run_00002",
        "timestamp": "2022-07-01T00:00:00",
        "device_name": "fake_armonk",
        "device_num_qubits": 1,
        "qubits_used": [0],
        "circuit_type": "ry_sweep",
        "control_theta": 1.23,
        "shots": 256,
        "counts": {"0": 200, "1": 56},
        "calibration": {"0": {}},
    }
    parsed = catalog._normalize(payload, fallback_id="x")
    assert parsed.t1_us is None
    assert parsed.readout_error is None
    assert parsed.control_theta == 1.23


def test_parse_file_raises_on_corrupt_json(run_dir):
    bad_file = run_dir / "run_bad.json"
    bad_file.write_text('{"run_id": "run_bad", "counts": {"0": 1')  # truncated
    with pytest.raises(catalog.CorruptRunFile):
        catalog.parse_file(bad_file)


def test_parse_file_uses_cache(run_dir, monkeypatch):
    good_file = run_dir / "run_cached.json"
    _write(good_file, {
        "run_id": "run_cached", "timestamp": "2024-01-01T00:00:00",
        "device_name": "fake_athens", "device_num_qubits": 5,
        "qubits_used": [0], "circuit_type": "ry_sweep",
        "control_theta": 0.5, "shots": 100,
        "counts": {"0": 60, "1": 40}, "calibration": {"0": {"t1_us": 60.0}},
    })
    first = catalog.parse_file(good_file)

    calls = {"count": 0}
    real_loads = json.loads

    def counting_loads(*args, **kwargs):
        calls["count"] += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counting_loads)
    second = catalog.parse_file(good_file)

    assert first == second
    assert calls["count"] == 0  # cached, never re-parsed the raw JSON


def test_fidelity_estimate_and_error_bar():
    counts = {"00": 480, "11": 20, "01": 0, "10": 0}
    fidelity = catalog._fidelity_estimate(counts, shots=500)
    err = catalog._error_bar(counts, shots=500)
    assert fidelity == 0.96
    assert 0 <= err < 0.05


def test_fidelity_estimate_handles_empty_counts():
    assert catalog._fidelity_estimate({}, shots=0) == 0.0
    assert catalog._error_bar({}, shots=0) == 0.0


def test_binomial_confidence_interval_bounds():
    low, high = catalog.binomial_confidence_interval(successes=480, shots=500)
    assert 0.0 <= low <= 0.96 <= high <= 1.0


def test_scan_builds_catalog_and_flags_corrupt(run_dir, tmp_path):
    _write(run_dir / "run_a.json", {
        "run_id": "run_a", "timestamp": "2024-01-01T00:00:00",
        "device_name": "fake_mumbai", "device_num_qubits": 27,
        "qubits_used": [0, 1], "circuit_type": "bell_pair",
        "control_theta": None, "shots": 100,
        "counts": {"00": 48, "11": 47, "01": 3, "10": 2},
        "calibration": {"0": {"t1_us": 150.0, "readout_error": 0.02}},
    })
    (run_dir / "run_b.json").write_text('{"run_id": "run_b", not valid json')

    db_path = tmp_path / "catalog.db"
    report = catalog.scan(data_dir=run_dir, db_path=db_path)

    assert report.files_scanned == 2
    assert report.runs_cataloged == 1
    assert report.corrupt_files == 1

    rows = catalog.query_runs(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["device_name"] == "fake_mumbai"

    corrupt = catalog.corrupt_files(db_path=db_path)
    assert corrupt[0]["source_file"] == "run_b.json"


def test_reads_on_unscanned_db_return_empty(tmp_path):
    # The dashboard (and any client) can hit the read endpoints before the
    # first scan has run, against a DB whose schema does not exist yet.
    # Reads must create the empty schema and return nothing, not raise
    # sqlite3.OperationalError: no such table.
    db_path = tmp_path / "fresh.db"
    assert catalog.query_runs(db_path=db_path) == []
    assert catalog.device_summary(db_path=db_path) == []
    assert catalog.corrupt_files(db_path=db_path) == []
    assert catalog.get_run("missing", db_path=db_path) is None


def test_query_runs_filters_by_device(tmp_path):
    db_path = tmp_path / "catalog.db"
    run_dir2 = tmp_path / "runs2"
    run_dir2.mkdir()
    _write(run_dir2 / "r1.json", {
        "run_id": "r1", "timestamp": "2024-01-01T00:00:00", "device_name": "fake_athens",
        "device_num_qubits": 5, "qubits_used": [0], "circuit_type": "ry_sweep",
        "control_theta": 0.1, "shots": 10, "counts": {"0": 7, "1": 3}, "calibration": {"0": {}},
    })
    _write(run_dir2 / "r2.json", {
        "run_id": "r2", "timestamp": "2024-01-02T00:00:00", "device_name": "fake_sherbrooke",
        "device_num_qubits": 127, "qubits_used": [0], "circuit_type": "ry_sweep",
        "control_theta": 0.2, "shots": 10, "counts": {"0": 5, "1": 5}, "calibration": {"0": {}},
    })
    catalog.scan(data_dir=run_dir2, db_path=db_path)

    athens_only = catalog.query_runs(db_path=db_path, device_name="fake_athens")
    assert len(athens_only) == 1
    assert athens_only[0]["run_id"] == "r1"

import json

from fastapi.testclient import TestClient

from app import catalog


def _seed_runs(run_dir):
    (run_dir / "run_x.json").write_text(json.dumps({
        "run_id": "run_x", "timestamp": "2024-01-01T00:00:00",
        "device_name": "fake_washington", "device_num_qubits": 127,
        "qubits_used": [0, 1], "circuit_type": "bell_pair",
        "control_theta": None, "shots": 200,
        "counts": {"00": 95, "11": 90, "01": 8, "10": 7},
        "calibration": {"0": {"t1_us": 70.0, "readout_error": 0.015}},
    }))


def test_api_full_flow(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs"
    run_dir.mkdir()
    db_path = tmp_path / "catalog.db"
    _seed_runs(run_dir)

    monkeypatch.setattr(catalog, "DATA_DIR", run_dir)
    monkeypatch.setattr(catalog, "DB_PATH", db_path)

    from app.api import app
    client = TestClient(app)

    scan_resp = client.post("/scan")
    assert scan_resp.status_code == 200
    body = scan_resp.json()
    assert body["runs_cataloged"] == 1

    runs_resp = client.get("/runs")
    assert runs_resp.status_code == 200
    runs = runs_resp.json()
    assert len(runs) == 1
    assert runs[0]["device_name"] == "fake_washington"

    detail_resp = client.get(f"/runs/{runs[0]['run_id']}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert "fidelity_ci_95" in detail
    assert detail["fidelity_ci_95"]["low"] <= detail["fidelity_estimate"] <= detail["fidelity_ci_95"]["high"]

    missing_resp = client.get("/runs/does-not-exist")
    assert missing_resp.status_code == 404

    summary_resp = client.get("/devices/summary")
    assert summary_resp.status_code == 200
    assert summary_resp.json()[0]["device_name"] == "fake_washington"

    corrupt_resp = client.get("/corrupt-files")
    assert corrupt_resp.status_code == 200
    assert corrupt_resp.json() == []

"""
Generate legacy-style measurement-run files from REAL IBM Quantum
hardware calibration data.

Qiskit ships calibration snapshots ("fake backends") taken from real
IBM Quantum processors at points in their operating history: real
per-qubit T1/T2 times, gate errors, and readout errors, spanning
several real hardware generations from 5-27 qubit Falcon-era chips up
to 127-qubit Eagle-era chips like fake_sherbrooke. This script runs
real circuits through each real device's actual noise model (via
Qiskit Aer) and records the result the way a lab's internal logging
system would, with the genuine differences in what each hardware
generation's calibration data makes available standing in for the
schema drift an older, legacy data archive naturally has.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator
from qiskit_ibm_runtime.fake_provider import FakeProviderForBackendV2

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "runs"

# Spans several real IBM hardware generations, smallest/oldest to
# largest/newest, which is where the genuine schema differences come from.
DEVICE_NAMES = [
    "fake_armonk",      # 1 qubit
    "fake_athens",       # 5 qubit
    "fake_santiago",     # 5 qubit
    "fake_mumbai",       # 27 qubit, Falcon
    "fake_kolkata",      # 27 qubit, Falcon
    "fake_washington",   # 127 qubit, Eagle
    "fake_sherbrooke",   # 127 qubit, Eagle r3
]


def _qubit_calibration(backend, qubit: int) -> dict:
    """Pull whatever real calibration fields this specific device's
    target actually reports for this qubit. Older/smaller devices
    report less than newer ones, which is genuine, not simulated."""
    cal: dict = {}
    try:
        props = backend.target.qubit_properties[qubit]
        if props.t1 is not None:
            cal["t1_us"] = round(props.t1 * 1e6, 4)
        if props.t2 is not None:
            cal["t2_us"] = round(props.t2 * 1e6, 4)
        if props.frequency is not None:
            cal["frequency_ghz"] = round(props.frequency / 1e9, 6)
    except Exception:
        pass
    try:
        meas = backend.target["measure"][(qubit,)]
        if meas.error is not None:
            cal["readout_error"] = round(meas.error, 6)
    except Exception:
        pass
    for gate_name in ("sx", "x", "rz"):
        try:
            g = backend.target[gate_name][(qubit,)]
            if g.error is not None:
                cal[f"{gate_name}_error"] = round(g.error, 6)
                break
        except Exception:
            continue
    return cal


def _bell_pair_circuit() -> QuantumCircuit:
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def _ry_sweep_circuit(theta: float) -> QuantumCircuit:
    qc = QuantumCircuit(1, 1)
    qc.ry(theta, 0)
    qc.measure(0, 0)
    return qc


def generate(n_runs: int = 300, seed: int = 7, malformed_fraction: float = 0.03) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    start = datetime(2022, 6, 1)

    fp = FakeProviderForBackendV2()
    all_backends = {b.name: b for b in fp.backends()}
    backends = {name: all_backends[name] for name in DEVICE_NAMES if name in all_backends}
    simulators = {name: AerSimulator.from_backend(b) for name, b in backends.items()}

    written = 0
    for i in range(n_runs):
        device_name = rng.choice(list(backends.keys()))
        backend = backends[device_name]
        sim = simulators[device_name]
        ts = start + timedelta(days=rng.randint(0, 850), hours=rng.randint(0, 23))
        run_id = f"run_{i:05d}"
        shots = rng.choice([256, 512, 1024])

        use_bell = backend.num_qubits >= 2 and rng.random() < 0.5
        if use_bell:
            qc = _bell_pair_circuit()
            qubits_used = [0, 1]
            theta = None
        else:
            theta = round(rng.uniform(0, 3.14159265), 4)
            qc = _ry_sweep_circuit(theta)
            qubits_used = [0]

        tqc = transpile(qc, sim, seed_transpiler=seed + i)
        result = sim.run(tqc, shots=shots, seed_simulator=seed + i).result()
        counts = result.get_counts()

        calibration = {q: _qubit_calibration(backend, q) for q in qubits_used}

        payload = {
            "run_id": run_id,
            "timestamp": ts.isoformat(),
            "device_name": device_name,
            "device_num_qubits": backend.num_qubits,
            "qubits_used": qubits_used,
            "circuit_type": "bell_pair" if use_bell else "ry_sweep",
            "control_theta": theta,
            "shots": shots,
            "counts": counts,
            "calibration": calibration,
        }

        malformed = rng.random() < malformed_fraction
        fname = DATA_DIR / f"{run_id}.json"
        if malformed:
            raw = json.dumps(payload)
            fname.write_text(raw[: len(raw) // 2])
        else:
            fname.write_text(json.dumps(payload))
        written += 1

    return written


if __name__ == "__main__":
    count = generate()
    print(f"wrote {count} measurement runs (real IBM device calibration + noisy simulation) to {DATA_DIR}")

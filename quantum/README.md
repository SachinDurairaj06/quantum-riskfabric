# quantum/ — the quantum ML challenger (owner: C)

Research, simulated exactly. It never changes a payment decision. Spec: `prd/05-C-quantum-ml.md`.

## Layout
| Path | What it is |
|---|---|
| `core/circuit.py` | QFM-1: 8 qubits on a line, 2 layers, RY/RZ encoding plus XX+YY+ZZ neighbour couplings. Exact statevectors in numpy, the projected features `phi` (15 values), the fidelity kernel, finite-shot sampling, and the same circuit in Qiskit |
| `core/preprocess.py` | Standardise, PCA to 8, scale to [0, 1]. Saved as JSON, so there are no pickles on s390x |
| `core/artifact.py` | Frozen QP-1 and QK-1 models as JSON, applied with numpy only |
| `research/models.py` | QK-1, QP-1 and QP-1a, and controls K0 to K4. Each matched pair gets 9 configurations |
| `research/metrics.py` | Recall at analyst capacity, PR-AUC, the paired account bootstrap |
| `research/gate.py` | The pre-registered decision rule and the `GATE.md` text |
| `research/evaluate.py` | The commands below |
| `worker/` | The live shadow worker for the VM (`python -m quantum.worker`) |
| `models/` | Frozen artifacts (committed, each under 1 MB), written by `evaluate test` |
| `results/` | One JSON file per model and seed, `summary.json`, `tuning.json`, `worker_latency.json` |

## How to run (prd/05 Part 4)
```bash
python -m quantum.research.evaluate checks                   # Q-02: must print six PASS lines
python -m quantum.research.evaluate toy                      # Q-03: QK-1 and K1 both above 0.9
python -m quantum.research.evaluate tune                     # Q-05: needs A's export in $RF_DATA/features/
python -m quantum.research.evaluate freeze                   # writes PROTOCOL.md; review it, then commit it
git add quantum/PROTOCOL.md && git commit -m "Q-06: freeze the quantum protocol"
#   no git on the machine (the LinuxONE VM)? seal it instead: records its SHA-256 and the time
python -m quantum.research.evaluate seal
python -m quantum.research.evaluate test                     # Q-07: refuses if PROTOCOL.md is not committed or sealed
python -m quantum.research.evaluate bench                    # on the VM: worker latency (needs the models `test` wrote)
python -m quantum.research.evaluate test                     # again, so the gate reads the measured latency
RF_QML_MODE=shadow python -m quantum.worker                  # Q-09: started by D's deploy/start.sh
```

**Rules the code enforces:**
- `test` refuses to run if `PROTOCOL.md` has local changes or was never committed. `--allow-uncommitted` exists for dry runs only, and makes the gate say VOID.
- `test` refuses if A's input files changed after `freeze`. It compares SHA-256 hashes.
- The verdict in `GATE.md` is computed by `research/gate.py`. Nobody writes it by hand.
- The worker refuses `RF_QML_MODE=rerank` unless `GATE.md` says PASS with rerank allowed.
- N1, the negative control, is QP-1 trained on shuffled labels, averaged over 20 shuffles. A single shuffle isn't enough: its random weights can still line up with the informative classical score.

## Evidence so far (laptop, 2026-10-17)
- **C-15 part 5:** numpy and Qiskit statevectors agree to a maximum difference of 7.8e-16 on 100 random inputs (`evaluate checks`).
- **Q-03 toy run:** QK-1 and K1 both reach 1.000 accuracy on the two-blob problem.
- **Pipeline:** the whole pipeline (tune, freeze, test, gate, artifacts, worker) runs end to end on synthetic data in `quantum/tests/`. **No real result exists yet.** Real numbers need A's feature export from HI-Small.
- **Not checked yet:** whether `pip install qiskit` works on the VM is D's pre-flight question. The worker doesn't need Qiskit: its live backend is `numpy-statevector`.

## Tests
`python -m pytest quantum`. This needs `pytest` and `fakeredis` (test only).

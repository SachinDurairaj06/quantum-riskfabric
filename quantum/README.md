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
python -m quantum.research.evaluate control                  # gate calibration self-test (optional, see below)
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

## Positive control (`control`) and the complexity sweep (`sweep`)
Synthetic data, **not bank data and not a fraud result**. Both answer one question: when the pattern behind the label
really has quantum structure, does the challenger find it, and does the gate say so?

- `python -m quantum.research.evaluate control` generates data whose label is a linear function of the feature map's
  own 2-qubit observables `phi`, then runs the *same* tune, freeze, seal and test gate on it, into
  `quantum/results/control_summary.json` (shown in the console under "Where the quantum challenger gains").
- `python -m quantum.research.evaluate sweep` repeats the matched comparisons while the label moves from a basic
  pattern (alpha 0: a plain linear rule on the circuit's 8 inputs) to the entangled pattern (alpha 1), into
  `quantum/results/advantage_sweep.json`. Every point uses the gate's method: 9-configuration tuning on validation only,
  3 seeds, recall at analyst capacity on the time-ordered test split, paired account bootstrap.

**Fixed (2026-10-18):** the control used to compute its labels with a preprocessor fitted on all rows, while the gate
fits it on the validation split. Its feature columns were independent, so after standardising, PCA had no preferred
axes and every fit picked a different rotation: the labels no longer lined up with the circuit's inputs, and even the
true model could not learn them (QP-1 lost to K4 by 7 to 10 points). The labels now live in the gate's own input frame,
and the generator mixes its latent factors so the PCA axes and signs are the same on every slice
(`test_control_labels_live_in_the_pipelines_input_frame`).

**Measured (laptop, 2026-10-18, seed 20250):**
- Positive control: **PASS**. QP-1 beat K4 by **+14.4, +20.0 and +16.3 points** of recall at analyst capacity on seeds
  1, 2 and 3 (95% intervals 12.0 to 18.7, 16.7 to 24.0, 13.5 to 20.2), 90.4% recall against 73.5% (means over the seeds). The negative control
  stayed at a random ordering (N1 11.7%, 10.8%, 11.9% against 10.0%). QK-1 did **not** beat its twin K1 (−0.4 to −0.9
  points): an RBF kernel learns this pattern as well as the fidelity kernel does.
- Sweep, QP-1 minus K4:

  | alpha (0 = basic, 1 = entangled) | QP-1 recall | K4 recall | QP-1 − K4, mean of 3 seeds | 95% range over seeds | rule met |
  |---|---|---|---|---|---|
  | 0.00 | 66.5% | 90.9% | −24.4 pts | −29.4 to −20.0 | no |
  | 0.25 | 72.0% | 90.3% | −18.3 pts | −22.0 to −13.2 | no |
  | 0.50 | 79.3% | 84.9% | −5.6 pts | −10.0 to +0.4 | no |
  | 0.75 | 89.4% | 77.4% | **+12.0 pts** | +6.4 to +18.0 | yes, every seed |
  | 1.00 | 90.4% | 73.5% | **+16.9 pts** | +12.2 to +24.0 | yes, every seed |

  QK-1 against K1 stayed within ±1.2 points at every alpha (rule never met). Random order catches 10.0%.
- Reading: on a basic pattern the classical twin wins clearly; the quantum feature map pays off only once the
  pattern is the entangled kind. That is why the bank-data verdict is a real test, not a formality.

None of this is a quantum computational advantage: 8 qubits are simulated exactly on a classical machine.

## Tests
`python -m pytest quantum`. This needs `pytest` and `fakeredis` (test only).

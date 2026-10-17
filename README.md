# Quantum RiskFabric

*A payment shield for banks on IBM LinuxONE. Load can't buy a weaker check.*

IBM Z Datathon 2026 · Track: Real-Time AI for Critical Decisions · Team: TODO (team name)

> **The payment path is classical.** The quantum component is an experimental challenger that scores held payments in shadow and never changes a decision. Result: TODO (C), the statement from `quantum/GATE.md`, word for word.

> Status: scaffold. Sections marked TODO are filled during the sprint by the person named. Never write a number here that was not measured. The specification is in [prd/](prd/00-README.md).

## The problem (TODO: E)
Attackers can slow a bank's fraud check down on purpose. A late check still needs an answer, and the usual fallbacks are flat: approve below a limit set in advance (as card networks' stand-in processing does, within issuer limits) or approve everything. A flat fallback ignores how risky the account is, so a flood can buy approvals for fraud hidden inside it. Edge defences don't see which payments were fraud, and fraud engines don't see the flood. One sourced incident goes here.

## Our solution (TODO: E)
- It sits alongside the bank's existing systems; the bank's own fraud engine keeps authority in Shield mode.
- Abusive traffic is shed at the edge before it costs the bank's engine any time.
- The bank's engine gets cluster signals it can't see on its own.
- A late decision goes to stand-in rules by account risk tier, never to approve-on-timeout or a flat limit.
- One full-strength classical model (graph features plus gradient boosting) scores every payment in shadow, at every load.
- A hash-chained ledger anyone can verify, and the bank's books as the independent record of money moved.
- An experimental quantum-ML challenger, tested against matched classical controls under a pre-registered protocol.

## How it sits alongside a bank (TODO: E)
The two diagrams and the adoption phases from [prd/01-product.md](prd/01-product.md) section 4.

## Architecture (TODO: E and D)
The whole chain, as the demo runs it:
```
lab ──> perimeter :8000 ──> edge :8080 ──> Shield router :8001 ──> bank :9000 ──> books
        CDN, WAF,           limits and      deadline budget,         channel, fraud   vault: sealed IBM
        API gateway         cluster         cluster signals,         engine, books    sample data
        (stand-in)          actions         stand-in rules
                               ▲
                            Sentinel (cluster detection), ledger, quantum challenger in shadow
```
| Folder | What it is |
|---|---|
| `perimeter/` | A stand-in for the bank's existing perimeter: a Cloudflare-style CDN and DDoS layer, an AWS-style WAF and an API gateway. Its request ceiling comes only from the capacity measured on the host |
| `devstack/` | Runs the whole chain on one laptop, with a Python stand-in for the HAProxy edge |
| `decision/` | The Shield router and decision service, with the stand-in rules |
| `sentinel/` | Finds coordinated clusters every second |
| `bank/` | The synthetic bank: customer channel, incumbent fraud engine (rules R0 to R5, fixed capacity) and core books |
| `vault/` | The bank's sealed data store: IBM's Jupyter Lab sample files for LinuxONE, hashed, verified and turned into the bank's accounts |
| `ledger/` | The hash-chained decision ledger and its verifier |
| `console/` | The live console and the scoreboard |
| `quantum/` | The experimental quantum challenger and its gate |
| `lab/` | The traffic lab: labelled synthetic traffic to 127.0.0.1 only, at a capped rate |
| `eval/` | Checks, the scenario runner and the metrics |

## Run the whole chain on a laptop
1. Copy IBM's Jupyter Lab sample files ([linuxone-community-cloud/jupyter-lab-ml](https://github.com/linuxone-community-cloud/jupyter-lab-ml); the files come with its Jupyter Lab environment on the LinuxONE VM) into `data/vault/`. Keep `sample_data/` as a subfolder. Git ignores `data/`.
2. Run:
   ```bash
   python -m vault seal
   python -m vault build
   python -m devstack demo --system shield
   ```
   The demo starts every service on 127.0.0.1, sends the normal-day scenario through the perimeter for 90 s at a capped rate, prints the numbers and stops. `--system bank_only` and `--system bank_realistic` run the same chain without RiskFabric.

A laptop run is a working check, not a measurement. Results come from the LinuxONE VM.

## Results (TODO: C)
From `eval/RESULTS.md`:
- the headline: bank + Shield against the realistic bank (the bank with its usual per-fingerprint limits, velocity rules and a bounded timeout fallback) and against the bank alone, in S4, S5 and S6, with S4b alongside. Each number says which baseline it is against;
- the evaluation matrix;
- the capacity table.

Every number lists the command that produced it. The live scoreboard is at `/scoreboard` on the console.

## The quantum challenger (TODO: C)
- **What it is:** an 8-qubit feature map; a quantum kernel classifier and a projected-feature re-ranker for held payments.
- **What it's compared with:** matched classical controls with the same inputs and tuning budget.
- **The protocol:** committed before testing.
- **The verdict:** from `quantum/GATE.md`.
- **Where it runs:** on LinuxONE, as an exact statevector simulation.

Wording follows [prd/05-C-quantum-ml.md](prd/05-C-quantum-ml.md) Part 7.

## How to run (TODO: D and E)
The exact commands, from a clean LinuxONE VM to a running stack, for each system. See `deploy/README.md`.

## IBM Z and LinuxONE usage (TODO: D)
- Hardware and OS (from `uname -m` and `lscpu`).
- Whether the AI accelerator was present.
- Which IBM software we used: Snap ML and the Graph Feature Preprocessor, plus Qiskit on s390x if it ran.
- What ran where.

Screenshots are in `docs/screenshots/`.

## Open-source resources (TODO: E)
Keep only what we actually used.
- [HAProxy](https://www.haproxy.org/)
- [Valkey](https://valkey.io/)
- [FastAPI](https://fastapi.tiangolo.com/) and [uvicorn](https://www.uvicorn.org/)
- [httpx](https://www.python-httpx.org/)
- [scikit-learn](https://scikit-learn.org/) and [LightGBM](https://lightgbm.readthedocs.io/)
- [IBM Z Accelerated for Snap ML](https://github.com/IBM/ibmz-accelerated-for-snapml)
- [IBM Transactions for Anti Money Laundering (AML)](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml)
- [IBM LinuxONE Community Cloud Jupyter Lab samples](https://github.com/linuxone-community-cloud/jupyter-lab-ml) (the vault's data)
- [pandas](https://pandas.pydata.org/) and [numpy](https://numpy.org/)
- [fakeredis](https://github.com/cunla/fakeredis-py) (tests, and devstack's in-memory Valkey on laptops)
- [Qiskit](https://github.com/Qiskit/qiskit) and [Qiskit Aer](https://github.com/Qiskit/qiskit-aer) (quantum challenger)
- [pytest](https://docs.pytest.org/) and [Podman](https://podman.io/) if used

## AI assistance (TODO: E)
This project was built by a team of five. We used AI coding assistants for parts of the implementation. All design decisions, contracts and measurements were made and verified by the team.

## Team (TODO: E)
Names and roles.

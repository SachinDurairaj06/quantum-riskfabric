# lab/ — traffic lab (owner: B)

A load-and-replay harness for our own stack: labelled synthetic traffic, sent to `127.0.0.1` only, at a capped rate. Spec: `prd/04-B-traffic-lab-and-sentinel.md` Part 1.

## Run
```bash
python -m lab run --scenario S4 --run-id s4-shield-01 --system shield --base-rps 200 --dry-run   # plan only
python -m lab run --scenario S4 --run-id s4-shield-01 --system shield --base-rps 200
```
- **Targets:** `http://127.0.0.1:8080` plus `RF_PORT_OFFSET`. The host is a constant in `lab/config.py`.
- **Rates:** set relative to R (`--base-rps`) and scaled by `RF_LAB_RATE`. The cap is `MAX_TOTAL_RPS` (2,000 per process); a plan above it is refused with exit code 3.
- **Exit codes:**
  - 0: the run is valid;
  - 2: the lab achieved under 95% of the offered rate, so the runner marks the run invalid;
  - 3: refused.
- **Output:**
  - the truth file is `$RF_DATA/truth/<run_id>.jsonl` (CONTRACTS section 10);
  - a JSON summary goes to stdout.

## Scenarios
The `scenarios/` files are S1, S2, S3, S4, S4b, S5 and S6, each 210 s with a 30 s warm-up (prd/02 section 3). Result seeds are 11 to 17; tune Sentinel with `--seed 1` only. `faults` in these files are read by C's runner; the lab never applies faults.

## Populations
| Kind | What it is |
|---|---|
| `legit` | Replay rows with label 0, from a stable synthetic customer per account. 50 fingerprints; the bank app has 40%. 30 networks; 80% are known clients. They complete VERIFY with probability 0.97 |
| `launder` | Replay rows with label 1. In S4 and S6 the trickle reuses the attacker's identities; in S4b it uses the account's own clean customer identity |
| `bot` | 2,000 identities sharing 1 tool fingerprint and 3 networks, each IP unique and regular in timing, with unknown `lab-` accounts. They never verify. In S6 (`use_app_fp`) they borrow the bank app's own fingerprint, so the fingerprint alone no longer tells them from customers |
| `cardtest` | Amounts of 5.00 or less from a small group of real accounts, in bursts |

Fingerprints of every kind look alike (`fp-` plus 8 hex digits), so Sentinel can't read labels from names.

## Measured
- **Laptop smoke test (2026-10-17):** 60 requests/s for 15 s against the decision service directly, 899 requests, all answered, one truth line each.
- **Not measured yet:** the achieved rates on the VM (L-11). Measure them there and add them here.

## Tests
`python -m pytest lab`. Uses a fake edge, with no sockets.

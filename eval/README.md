# eval/ — checks, scenario runner and results (owner: C)

Specs: `prd/02-evaluation-matrix.md`, `team/CHECKS.md`.

## Checks
```bash
eval/checks/run_all.sh                 # every check, in order; exit 0 only if nothing failed
python -m eval.checks.run C-05 C-14    # some checks only
```
Each check prints `PASS`, `FAIL` (with the reason) or `SKIP` (with why). A SKIP is never a pass.

Parts that can't be automated safely say so in their output and are run by hand:
- **C-06 block map:** needs Sentinel stopped, because Sentinel's next `threat` entry replaces the test block.
- **C-10 delay and failure switches:** the router doesn't forward `X-Mock-*` headers.
- **C-13 idle against stress:** run by hand.
- **C-15 worker on against off:** run by hand.

Check settings: `RF_CHECK_SYSTEM`, `RF_CHECK_P99_MS` (100 at CP1, 50 from CP2) and `RF_CHECK_STREAMS`.

## Scenario runner
```bash
python -m eval.scenarios.runner dry --base-rps 200 --should      # the plan, nothing runs
eval/scenarios/run_all.sh 200                                     # the MUST matrix (36 runs), then the summary
python -m eval.scenarios.runner run --scenario S4 --system shield --repeat 1 --base-rps 200
python -m eval.scenarios.runner run --scenario S5 --system bank_realistic --repeat 1 --base-rps 200 --bank-fail-mode closed
python -m eval.scenarios.runner capacity --base-rps 200          # S3 mix on Shield, capacity table
python -m eval.scenarios.summary                                  # rebuild summary.json and RESULTS.md
```
The `--bank-fail-mode` line is the fail-closed sensitivity check (prd/02 section 6, rule 8); its run id ends in `-closed`.

**Systems** (prd/02 section 2). The bank's engine (rules, model, capacity) is the same in all of them.
- `bank_only`: the status quo, a per-IP limit at the edge and a channel that approves on timeout.
- `bank_realistic`: what a bank usually has before its engine. Per-IP and per-fingerprint limits at the edge (`cfg:fp_limit_rps` = 2.4R), and a bounded timeout fallback: known accounts up to `bank:cfg:fallback_limit` are approved, larger amounts get VERIFY, unknown accounts HOLD. It has none of our cross-identity clustering, enrichment, tiers, shadow scoring or ledger.
- `shield`, `standalone`, `submitted_design`: RiskFabric in front of the same bank.

**MUST matrix**, 3 runs per cell: `shield` on S1 to S6; `bank_realistic` on S1, S4, S5 and S6; `bank_only` on S1 and S4. `--should` adds the other cells once (`runner.SHOULD`).

Per run, the runner does this:
1. `deploy/stop.sh`, then `deploy/reset.sh --yes`. Then it sets the bank:
   - capacity by the 4R rule (`bank:cfg:service_ms = slots * 1000 / 4R`);
   - `bank:cfg:fail_mode`: `bounded` for `bank_realistic`, `open` otherwise, or the `--bank-fail-mode` value;
   - `bank:cfg:fallback_limit` = `cfg:standin_low_limit` (default 10000), so the bounded fallback and our stand-in differ only in the risk tiers;
   - for `bank_realistic` only, `cfg:fp_limit_rps` = 2.4R, which D's `start.sh` reads.
2. `deploy/start.sh --system <id>`.
3. It tails `edge.log`, `decisions`, `bank.log` and `threat` into `$RF_DATA/results/raw/<run_id>/`, because the streams are capped at 100,000 entries.
4. It starts the lab and schedules the faults.
5. It waits 5 s for settles, copies the ledger and the postings, and verifies the ledger (the two bank systems have none).
6. It computes M-01 to M-22 (`scenarios/metrics.py`) and writes `results/<scenario>__<system>__<stamp>.json`.

A run that goes wrong is written with `valid: false` and its reason, and is never deleted.

**Faults:**
- `bank_brownout` is applied by the runner.
- `cpu_stress` uses `stress-ng`.
- `lo_delay` needs D's loopback-only script in `scenarios/faults.json`. Until then, S2 runs are marked invalid with "fault lo_delay not applied".

## The scoreboard feed
`results/summary.json` holds the medians, min and max, the run ids, the command, and a pass mark for each target. E's `/scoreboard` renders it as it is. M-13 (Shield overhead) is measured against `bank_realistic` when S1 has that cell, else against `bank_only`; the cell's `overhead_reference` says which. The targets against `bank_only` stay; against `bank_realistic` we only report, with no target set in advance. The model section reads `scoring/report.json` if A writes it; otherwise the scoreboard shows "not run".

## Tests
`python -m pytest eval`. Covers metrics on a small synthetic run, the summary, targets, the runner plan, capacity, and the offline checks.

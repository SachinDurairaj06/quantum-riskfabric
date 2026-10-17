# devstack/ — the whole bank chain on one laptop (owner: C)

For machines without HAProxy or Valkey. One command starts the full chain, sends a short, capped scenario through it, prints the numbers and stops everything:

```
lab --> perimeter :8000 --> edge :8080 --> decision :8001 --> bank :9000 --> books, vault data
        CDN, WAF,           devstack/edge.py   Shield router   channel,
        API gateway         (stand-in for      (shield only)   engine,
                            D's HAProxy)                       books
```
Sentinel and the ledger writer run alongside for `shield`, and the console on 8500 for every system. Everything listens on 127.0.0.1; ports add `--offset`. It doesn't run D's health probe or the quantum worker, so checks C-04 (the `health` stream) and C-15 (`qml:` entries) need the VM.

## Run
```bash
python -m vault seal && python -m vault build       # once, after copying the IBM sample files into data/vault/
python -m devstack demo --system shield             # S1 for 90 s at R = 40, through the perimeter
python -m devstack demo --system bank_realistic --scenario S4 --base-rps 10 --seconds 150
python -m devstack up --system shield               # keep it running; Ctrl+C stops everything
```
- **Systems:** `bank_only`, `bank_realistic` or `shield`. Standalone needs A's scoring package, so it isn't offered yet.
- **Data:** `--data` (default `riskfabric/data`, which git ignores).
- **Valkey:** `--valkey` (default `RF_VALKEY_URL`). If nothing answers there, it starts an in-memory stand-in (fakeredis) for the run. If a real Valkey answers, a run clears RiskFabric's keys in that database, so it needs `--allow-reset`. Never point it at the shared stack.
- **Results:** each demo writes `data/results/dev/<run_id>.json`, never `eval/results/`, so a laptop run can't end up in the matrix.
- **Logs and pid files:** `data/run/`.

## Laptop sizing
- **Bank slots:** chosen so the 4R capacity rule gives a service time near 10 ms, as 8 slots do at R = 200 on the VM. With 8 slots at a laptop's low R, the service time would be close to the whole 100 ms deadline.
- **Rate limit:** each Python hop adds a few milliseconds. On a Windows laptop the chain stayed valid at about 50 requests per second in total; at about 150 the lab fell below 95% of its offered rate and marked the run invalid.

**A laptop run is a working check of the chain, not a measurement.** Numbers for the README come from the LinuxONE VM, with HAProxy and Valkey.

## The edge stand-in (`edge.py`)
It keeps the real edge's contract: the CONTRACTS section 3 headers, the per-IP limit, the cluster actions, the per-fingerprint limit for `bank_realistic`, primary, standby and bypass routing, and every `edge.log` field. It also removes internal headers a client tries to set (`X-Cluster`, `X-Cluster-Risk`, `X-Edge-Action`, `X-Client-IP`, `X-Deadline-Ms`, `X-Edge-Ts`); the HAProxy configs must do the same.

It differs from HAProxy on purpose in two ways. It reads Sentinel's latest `threat` entry itself every 0.5 s, so there's no applier. And throttle lets a cluster through at 5 requests per second, with 429 above that.

## Tests
`python -m pytest devstack`: headers, header stripping, limits, cluster actions, failover and bypass.

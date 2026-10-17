# decision/ — decision service and Shield router (owner: E)

Spec: `prd/07-E-shield-app-and-console.md`. Contracts: sections 3 to 9 and 11.

## Run
```bash
RF_MODE=shield RF_REPLICA=8001 python -m decision     # primary on 127.0.0.1:8001 (+RF_PORT_OFFSET)
RF_MODE=shield RF_REPLICA=8002 python -m decision     # standby on 8002: follows `payments`
python -m ledger.writer                               # the single ledger writer
python -m ledger.verify                               # exit 0 if the chain is valid
python -m console                                     # console and /scoreboard on 127.0.0.1:8500
```
**Settings.** These come from the variables in CONTRACTS section 2:
- `RF_MODE`, `RF_REPLICA`, `RF_VARIANT`, `RF_DEADLINE_MS`, `RF_ENGINE_URL`
- `RF_VALKEY_URL`, `RF_DATA`, `RF_PORT_OFFSET`

The service loads `scoring` (A's package) and `scoring/policy.json` at startup. If `scoring` is missing or its `load()` fails, the service still starts. `/health` then reports `model_loaded: false` (503 in Standalone mode), and every payment goes to stand-in rules. That is deliberate: a broken model never becomes an approval.

## Behaviour worth knowing
- **Shield path:** the engine's decision is returned unchanged, with `path` `engine`, `model` `bank-engine` and `score` set to the engine's own score. Our classical score arrives later as a `shadow` ledger record.
- **After responding:** settle, shadow score, `payments` and hold `features` run in a bounded background queue. A full queue is counted in `/stats` (`settle_failed`, `errors`) and never blocks a response.
- **Engine budget:** each engine call gets `deadline - elapsed - 10` ms, enforced with a hard timeout. A late engine gets stand-in, with `engine.timeout: true`.
- **Pending VERIFY:** payments awaiting step-up stay in memory for 60 s. HAProxy sends all traffic to the primary, so `/verify` lands on the same replica.
- **httpx logging:** httpx request logs are turned down to WARNING, because one line per bank call is too much at load.

## Not done yet
- **E-10, the downgrade baseline.** It needs A's fast-model hook (A-13). The contract doesn't name it yet. `RF_VARIANT=submitted_design` therefore refuses to start, rather than silently running the full model.

## Tests
`python -m pytest decision ledger console`. Needs `pytest` and `fakeredis` (test only). 28 tests passed on 2026-10-17.

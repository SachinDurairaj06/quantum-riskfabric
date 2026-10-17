# bank/ — the synthetic bank (owner: A)

The existing bank that RiskFabric sits alongside, in one FastAPI process on `127.0.0.1:9000` (plus `RF_PORT_OFFSET`). Spec: `prd/03` Part 2; contract: `team/CONTRACTS.md` sections 5, 6 and 11.

## Run
```bash
python -m vault build        # once: accounts, tiers and the replay from the sealed vault (see vault/README.md)
python -m bank load          # before every run: opening balances and tiers into Valkey
python -m bank               # serve
```
Settings come from `RF_VALKEY_URL`, `RF_DATA` and `RF_PORT_OFFSET`.

## The three parts
| Part | Endpoints | What it does |
|---|---|---|
| Channel | `POST /bank/pay`, `POST /bank/verify` | The bank's own front door. Runs the engine within `bank:cfg:deadline_ms`, measured from the edge's `X-Edge-Ts`; on timeout applies the fail mode; posts ALLOW itself; VERIFY waits up to 60 s for the step-up |
| Incumbent engine | `POST /engine/pay`, `GET /engine/health` | Rules R0 to R5 behind a fixed-capacity queue. The Shield router calls it directly |
| Books | `POST /bank/settle`, `GET /bank/books`, `GET /bank/health` | Posts only ALLOW, once per `txn_id`; postings in `$RF_DATA/bank/postings.jsonl`, balances in `bank:bal:{acct}` |

## Rules (`rules.json`, rules-v1, frozen at H8)
| Rule | Fires when | Outcome |
|---|---|---|
| R0 unknown account | `from_acct` isn't in `accounts.csv` | HOLD |
| R1 large amount | above 10,000 (retail) or 100,000 (business) | VERIFY |
| R2 velocity | at least 10 payments from the account within 60 s of payment time, this one included | HOLD |
| R3 card testing | at least 5 payments of 5.00 or less within 300 s | HOLD |
| R4 model | `raw-gbm-v1` at or above the bank's thresholds | HOLD or VERIFY |
| R5 enrichment | `X-Cluster-Risk` at least 0.8 (HOLD) or `X-Edge-Action: stepup` (at least VERIFY); only RiskFabric sends these | HOLD or VERIFY |

The strictest outcome wins; `reasons` and `bank.log` `rules` list every rule that fired. Windows use the payment's `ts` (dataset time).

**R4 is off until A-04 delivers `raw-gbm-v1`.** Until then the incumbent engine is rules only, `model_loaded` is false and `score` is null. Say so next to any bank-alone number.

## Capacity
`bank:cfg:slots` requests at a time (default 8), each holding its slot for `bank:cfg:service_ms` (an awaited sleep, so it costs no CPU). Capacity is about `slots x 1000 / service_ms` per second; C sets `service_ms` at H10 so capacity is 4R. Requests that find no free slot queue. A caller that gives up doesn't free the slot: the engine finishes the work, like a real one. Settings are re-read every 0.5 s, so the S5 brownout applies within 1 s.

## Fail modes (`bank:cfg:fail_mode`)
| Mode | Used by | Late engine means |
|---|---|---|
| `open` (default) | `bank_only`, the status quo | ALLOW |
| `closed` | the fail-closed sensitivity run | HOLD |
| `bounded` | `bank_realistic` (BK-11) | ALLOW for a known account up to `bank:cfg:fallback_limit` (default 10,000), VERIFY above it, HOLD for an unknown account |

Every timeout answer has `engine.timeout` true and a `bank.log` channel entry with `timeout=1`. `fail_open_count` and `fail_open_value` in `/bank/books` count the postings those answers caused, in any mode.

## Test switches (C-10)
On `POST /engine/pay`: `X-Mock-Delay-Ms` adds latency; `X-Mock-Fail: 1` answers 503.

## What the bank does not model
Overdrafts (balances may go negative), settlement networks, interest and fees, real KYC, chargebacks, and anything the sample data doesn't hold. All data is synthetic.

## Tests
`python -m pytest bank`: every rule, every fail mode including the three bounded cases, verify, settle idempotency, the capacity queue, the brownout switch, the test switches, `bank.log` fields and the contract shape.

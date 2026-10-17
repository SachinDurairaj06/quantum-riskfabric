# Sentinel rules (prd/04 SN-07)

All numbers live in `sentinel/rules.py`. This file explains them. **Status: draft until the H13 tuning freeze.** At the freeze, B commits the final values here and in `rules.py`, and C reviews them. Tuning uses lab seed 1 only; result runs use the seeds in `lab/scenarios/`.

## What Sentinel reads
- **`edge.log`, one entry per request:** `ts`, `req_id`, `ip`, `fp`, `asn`, `path`, `status`. It also uses `known` (`1` when the client sent `X-Client-Known: 1`). That field is an additive edge.log field D's log shipper must include; when it's missing, Sentinel treats the client as unknown.
- **`decisions`:** joined by `req_id`, to count VERIFY and HOLD per key.
- **`/verify` requests** in `edge.log` with status 200: these count as completed step-ups.

Sentinel never reads the truth file and never relies on how the lab names things.

## Per window (1 s), per key
Keys:
- `fp:<fingerprint>`
- `fp_asn:<fingerprint>|<asn>`

A key is a candidate when it has at least 10 requests and at least 2% of all requests in the window. (It was 20, which left clusters slower than 20 requests a second per key unseen.)

Score, with weights summing to 1:
| Part | Weight | Signal |
|---|---|---|
| Share jump | 0.30 | Share above the key's 5-minute baseline (EWMA of non-coordinated windows); 10 points above counts in full |
| Unknown clients | 0.20 | 1 minus the known-client share |
| No step-up completion | 0.20 | 1 minus VERIFY completion over the last 10 windows; 0.5 when there were fewer than 5 prompts |
| Scripted timing | 0.15 | Median per-IP gap coefficient of variation below 0.30 (at least 5 IPs with 3 or more gaps) |
| Few networks | 0.15 | Top network's share of the key above 15%, in full at 50% |

A window is **bad** for a key at a score of 0.60 or more.

## Customer protection
- A fingerprint whose known-client share is 50% or more is never acted on as a whole. The bank's own app is the example. The share used is the larger of the current window's and the fingerprint's baseline in normal windows, so an attack that borrows the app fingerprint can't strip its protection.
- **How the baseline is learned:** from every calm window where the fingerprint sent at least 3 requests (`LEARN_MIN_COUNT`) and scored below the bad threshold itself. So protection doesn't depend on the traffic rate, and an attack building up on the fingerprint can't wear the protection down before it's detected.
- For a protected fingerprint, Sentinel acts on `fp_asn` keys instead, and never above step-up.
- An `fp_asn` key is skipped when its whole (unprotected) fingerprint is already being acted on.

## Escalation (hysteresis)
| After | Action |
|---|---|
| 3 consecutive bad windows | throttle |
| 5 more bad windows | step-up |
| 5 more bad windows | block (never for protected keys) |
| 10 good windows | released |

Each step is at least as strict as the one before at the edge: step-up keeps the throttle's cap and adds `X-Edge-Action: stepup` to what passes (CONTRACTS section 11). Otherwise a cluster in step-up, and every protected key, which stops there, would reach the bank's engine at full rate.

While a cluster is active, `until` is the window time plus 60 s. A cluster missing from the latest `threat` entry is released by D's applier. `risk` is the key's score, clipped to 0 to 1, and is forwarded to the bank engine as `X-Cluster-Risk`. `decline_rate` is the key's HOLD share over the last 10 windows.

## Load level
- **COORDINATED:** any active cluster with a share of at least 10% in the window.
- **SURGE:** total requests more than 2 times the baseline, at least 20 distinct fingerprints, and at least 60% known clients.
- **NORMAL:** otherwise.

Baselines update only outside COORDINATED windows.

## Known limits
- Bot traffic that copies customer behaviour closely enough will be missed: completing step-up, known flags, irregular timing, many networks. SN-09 (the unseen-profile test) measures how much.
- Real fingerprints (for example TLS fingerprints) are richer than the lab's opaque strings.

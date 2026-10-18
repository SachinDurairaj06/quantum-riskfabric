# scoring/REPORT.md — model quality (owner: A, requirement A-12)

Numbers here are filled **on the LinuxONE VM with the real HI-Small dataset**, each with the command that produced it.
A run on synthetic look-alike data only checks that the pipeline works; those numbers are never written here or in the
README (project rule: never write a number that wasn't measured).

## How it was built
```bash
# on the VM, with HI-Small_Trans.csv present and RF_MODEL_BACKEND=lightgbm (or snapml)
python -m scoring train  --source hi_small --path $RF_DATA/replay/HI-Small_Trans.csv
python -m scoring export --source hi_small --path $RF_DATA/replay/HI-Small_Trans.csv
```
- Models: `gfp-gbm-v1` (graph + raw features) and `raw-gbm-v1` (raw features only), both on the train split.
- Features: the 14 named graph/behavioural features in `scoring/features.py` (`FEATURE_NAMES`), computed identically
  online and offline. Snap ML's Graph Feature Preprocessor replaces the pandas-window fallback on the VM; the feature
  names and order stay the same.
- Split: time-ordered 60/20/20 (train/val/test). No labels or future data as features.
- Thresholds: `policy.json` `hold_at`/`verify_at` tuned on validation to the hold-rate cap (`cfg:hold_cap_pct`, ~2%).

## Metrics on the time-ordered test split (fill on the VM)
| Model | Minority-class F1 | PR-AUC | Recall at top 1% |
|---|---|---|---|
| `gfp-gbm-v1` (ours) | _fill_ | _fill_ | _fill_ |
| `raw-gbm-v1` (plain baseline) | _fill_ | _fill_ | _fill_ |

Source of the table: `scoring/report.json`, written by `python -m scoring train`. The scoreboard reads it
(CONTRACTS section 12). Command for each number: the `train` command above.

## Latency and platform (fill on the VM)
- `score()` p50 / p95 / p99 over 1,000 replay rows, warm: _fill_ (check C-13).
- Graph build time at `load()`: _fill_.
- AI accelerator present (`grep -o nnpa /proc/cpuinfo`): _fill_.
- Backend used (`sklearn` / `lightgbm` / `snapml`) and versions: _fill_.
- If `snapml` did not install, the pandas-window graph-feature fallback was used — say so here.

## Dataset counts (fill at H1)
Rows, laundering rows, accounts, and the date range of HI-Small, from `python -m scoring train` (the `rows` and
`laundering` fields of its report). Account-id uniqueness across banks: `<From Bank>-<Account>` is used, so ids are
unique by construction.

# scoring/ — the classical risk engine (owner: A)

The full-strength model that scores every payment (CONTRACTS section 9; prd/03 Part 1). The decision service calls
`load()`, `score()`, `ingest()`; the quantum challenger is fed by the feature export here.

## Interface
```python
import scoring
scoring.load(model_dir)                       # once at startup; loads gfp-gbm-v1 and seeds the graph from history
out = scoring.score(payment, signals, 40)     # {score, reasons, model, feature_hash, latency_ms, features}
scoring.ingest(payment)                        # standby replica: add to the graph without scoring
scoring.load(model_dir, scoring.FAST_MODEL)    # the downgrade baseline's fast model (raw-gbm-v1), never by default
```
`score()` reads the live graph for this payment's features, scores, then adds the payment — the same features the
offline trainer and exporter compute, so the offline numbers describe the live system. No network calls; same answer
at any load.

## Build and export
```bash
python -m scoring train  --source <hi_small|cards|replay> --path FILE   # fit the models, tune policy.json
python -m scoring export --source <hi_small|cards|replay> --path FILE   # write the quantum candidates
```
- **Sources:** `hi_small` (IBM's `HI-Small_Trans.csv`, the VM), `cards` (the vault's sealed card file), `replay` (any
  `txn_id,ts,from_acct,to_acct,amount,currency,format,label` CSV).
- **Models** go to `$RF_DATA/models/` (`gfp-gbm-v1`, `raw-gbm-v1`, `history.csv`, `feature_stats.json`).
- **Export** writes `$RF_DATA/features/qml_candidates.csv` and `qml_capacity.csv` to exactly the columns the quantum
  gate reads (CONTRACTS section 13): the top 2% of each day by `gfp-gbm-v1` score, val and test, in time order.

## The model backend (one swap point)
`scoring/model.py` holds the backend behind one interface. Now: scikit-learn's histogram gradient boosting (installed
everywhere). On the VM, set `RF_MODEL_BACKEND=lightgbm` or `snapml` — nothing else changes, and those save a portable
text model (big-endian safe on s390x). The sklearn backend's joblib file is the laptop fallback only.

## Graph features
`scoring/features.py` computes 14 named features from a rolling payment graph: fan-in / fan-out counts and degrees,
scatter, a reciprocal-cycle flag, a new-counterparty flag, amount and timing. The same names and order whether they
come from this pandas-window fallback or Snap ML's Graph Feature Preprocessor on the VM.

## Tests
`python -m pytest scoring`. On SYNTHETIC look-alike data only (`scoring/tests/synth.py`): feature parity and no
leakage, the model interface, the `score()` contract, policy tuned to the cap, the export schema, and the real
handoff — the exporter's output run through the quantum gate's `tune`. Synthetic runs check the pipeline, never
accuracy; real metrics come from HI-Small on the VM.

# IEEE-CIS Fraud Detection — handoff for model-training takeover

Purpose: consolidate everything that was actually done on the IEEE-CIS (Vesta)
dataset so one person owns model training going forward. This reports **only what
exists in the repo** — where something was not measured or not checked, it says so.

Paths are relative to the `RiskRadar/` project root.

---

## 1. Every file involved

### Raw Kaggle inputs
- `ml/data/public/train_transaction.csv`
- `ml/data/public/train_identity.csv`
- `ml/data/public/test_transaction.csv`
- `ml/data/public/test_identity.csv`

### Derived data
- `ml/data/public/ieee_train_joined.csv` — transaction ⨝ identity, 590,540 rows, with `card_start_day` added
- `ml/data/public/ieee_test_joined.csv` — same for the test split (no labels)

### Code that loads / cleans / joins
- `ml/datasets/join_ieee.py` — joins the two files, selects columns, derives `card_start_day`
- `ml/datasets/ieee_cis.mapping.yaml` — column mapping for the **train** split (labelled)
- `ml/datasets/ieee_cis_test.mapping.yaml` — column mapping for the **test** split (no label field)
- `ml/byod/canonical.py` — turns a mapped file into canonical rows
- `ml/byod/mapping.py` — mapping loader / validator
- `ml/byod/capability.py` — decides which features/rules can even work on this data
- `ml/byod/report.py` — writes the `.json` / `.html` / `.alerts.csv` outputs

### Code that trains / evaluates
- `ml/evaluate_dataset.py` — CLI entrypoint (`suggest` / `run`); produces the single-split evaluation
- `ml/byod/evaluate.py` — the evaluation engine (features, arms, time split, metrics)
- `ml/datasets/ieee_model.py` — **trains the exported IEEE model** on every labelled train row
- `ml/datasets/ieee_benchmark.py` — walk-forward (4-fold) benchmark
- `ml/train.py` — `build_model()`, the shared model recipe
- `backend/riskradar/model/calibrated.py` — the model class (`TimeSplitCalibratedBooster`)
- `backend/riskradar/features/spec.py` — the feature list + spec version
- `backend/riskradar/features/` — feature computation (`compute_features`, `to_vector`, `PandasHistorySource`)
- `scripts/replay_dataset.py` — streams the mapped file through the live API (used for the test split)

### Outputs / artifacts
- `ml/artifacts/ieee-gbm-20260917.1351-ieee.joblib` — **the trained model** (918 KB)
- `ml/artifacts/evaluation-ieee-gbm-20260917.1351-ieee.json` — that model's report card
- `ml/artifacts/datasets/ieee_train.evaluation.json` / `.html` — single time-split evaluation
- `ml/artifacts/datasets/ieee_train_budget100.evaluation.json` / `.html` — same split at 100 alerts/day
- `ml/artifacts/datasets/ieee_benchmark.json` — walk-forward results (the only place ROC-AUC lives)
- `ml/artifacts/datasets/ieee_test.evaluation.json` / `.html` — test split, volumes only (no labels)
- `ml/artifacts/datasets/ieee_train.alerts.csv`, `ieee_test.alerts.csv` — flagged rows
- `ml/artifacts/datasets/ieee_*.features-*.npz` — cached feature matrices
- `ml/artifacts/datasets/ieee_replay.log`, `ieee_*.run.log`, `ieee_test_joined.replay-labels.jsonl`

There are **no notebooks**; everything is `.py`. Decision-log entries: **D81, D81a–D81e, D10f**
in `docs/decisions.md`.

> ⚠️ `ml/artifacts/riskradar-gbm-*.joblib` are **NOT** IEEE models — they are trained on the
> simulated bank corpus. Only `ieee-gbm-*` was trained on IEEE-CIS.

---

## 2. Exact feature list fed into the model

The model takes **21 engineered features** (`MODEL_FEATURE_NAMES`, feature spec `1.4.1`),
computed by `riskradar.features` from the mapped columns — **not** raw dataset columns:

```
amount_log10, amount_ratio_to_account_p95_30d, txn_count_1h_account,
approved_value_ratio_24h_vs_daily_mean_30d, failed_attempts_1h_account,
decline_rate_24h_account, distinct_beneficiaries_1h_account,
beneficiary_is_new_to_account, beneficiary_first_seen_days,
device_is_new_to_subject, account_age_days, days_since_account_activity,
failed_logins_1h_subject, device_bound_hours, credential_changed_hours,
sim_changed_hours, payee_added_minutes, region_is_new_to_subject,
card_present_count_1h_account, beneficiary_distinct_senders_24h, hour_of_day_local
```

### Did it include any V1–V339 columns?
**No — none of them.** The V columns are not in the pipeline at all: `join_ieee.py`'s
`KEEP` list does not select them, so they are not even present in `ieee_train_joined.csv`
(verified — the joined header contains `TransactionID, isFraud, TransactionDT,
TransactionAmt, ProductCD, card1–card6, addr1/2, dist1/2, P/R_emaildomain, C1–C14,
D1–D15, DeviceType, DeviceInfo, card_start_day` and no `V*`).

Why: policy **D10f** — "a column whose making cannot be read is not an input." The V1–V339
meanings were never published by Vesta, so they were excluded by rule, not tested.

### Critical caveat — how many features actually carry signal on IEEE
The capability audit in `ieee_train.evaluation.json` shows that on this dataset only
**2 of the 21 features actually vary**: `amount_log10` and `hour_of_day_local`. Every other
feature is constant or "not applicable" because IEEE-CIS has no per-customer history depth,
no login/SIM/device events, no incoming credits, no beneficiary, and no auth/decline field.
So although the model is nominally 21-dimensional, on IEEE it is effectively an
**amount + hour-of-day** model.

### What the "own columns" arm added (NOT in the exported model)
A separate *comparison* arm added Vesta's own engineered numeric columns —
**C1–C14, D1–D15, dist1, dist2** (31 columns) — on top of the 21. These are used only in the
"retrained with the dataset's own columns" / `plus_vesta_c_d` **ceiling** arm. They are **not**
in the shipped/exported `ieee-gbm` model.

---

## 3. Leakage check — what was and wasn't done

**On the V columns: no leakage test was run, because the V columns were never added to any
arm.** There is no experiment measuring whether adding V1–V339 causes a suspicious jump.

**On the C/D columns:** their effect was *measured* but was **not audited as leakage**. Adding
C1–C14 / D1–D15 / dist1–2 raises performance sharply:
- single split: PR-AUC lift 3.02 → **13.24**; (walk-forward ROC-AUC 0.755 → **0.873**)
- incident recall at 100/day: ~17% → **~49%**

This jump was recorded as a legitimate **"ceiling"** (Vesta's own engineered card-level
features, treated as point-in-time-correct), not investigated for target/look-ahead leakage.
**If the teammate intends to rely on the C/D columns, that jump has not been leakage-audited
and should be.**

**General leakage discipline that does exist (for context, on *other* datasets):**
- D10f rule bars unreadable "risk/score/rate/fraud" columns (this is what excludes V1–V339).
- D55a/D55b (a different, synthetic dataset) flagged label-copy columns (`fraud_technique`,
  `composite_risk`, …) and look-ahead aggregates (`tx_count_total`) as leakage and dropped them.
- D56a ran an explicit "remove-the-column, does performance collapse?" leakage test — but on the
  Nigerian dataset, not IEEE.

**`card_start_day`** (`TransactionDT//86400 − D1`) is documented as point-in-time-correct — a
fact about the account known at transaction time, not derived from the label.

---

## 4. Train/test split methodology

**Temporal, ordered by `TransactionDT` — never random.** (Random splits are explicitly rejected
in `byod/evaluate.py`: a random split lets the model see next month's fraud.)

Three related splits exist:

| Where | Method | Ratio / detail |
|---|---|---|
| Single-split eval (`ieee_train.evaluation.json`) | Time-ordered | Older **70%** train (413,378 rows) / newer **30%** test (177,162 rows); test window 2018-03-30 → 2018-05-31 (~62 days) |
| Walk-forward (`ieee_benchmark.json`) | 4 expanding time folds | learn days 0–60/0–90/0–120/0–150, test the next ~30 days each |
| Exported model (`ieee_model.py`) | Trained on **all** labelled rows | No test held out; its "evidence" is the single-split eval above |

**Validation set:** yes, internal to the model. `TimeSplitCalibratedBooster` further holds out
the **newest 20% (by time)** of whatever it's given to fit the isotonic calibrator; the booster
trains on the older 80%. So within the 70% train slice there is a time-ordered
train/calibration split. There is **no** third held-out validation set separate from the 30% test
in the single-split evaluation — the 30% is the test.

**Kaggle test split:** temporal (30 days after train ends), **no published labels** — it can only
be scored and replayed, not measured.

---

## 5. Every metric actually computed

**Accuracy was never computed** (correct, given a 3.5% base rate).

**ROC-AUC** — computed **only in the walk-forward benchmark** (`ieee_benchmark.json`):
- Risk Radar 21 features: mean **0.755** (range 0.746–0.760)
- + Vesta C/D columns: mean **0.873** (range 0.869–0.877)
- The single-split evaluation does **not** report ROC-AUC.

**PR-AUC** — computed in both. Reported as raw `pr_auc` and as `pr_auc_lift_over_random`
(PR-AUC ÷ base rate):
- single split, retrained (21 feat): PR-AUC **0.1043**, lift **3.02**
- single split, + C/D columns: PR-AUC **0.4576**, lift **13.24**
- single split, shipped-as-is model: PR-AUC 0.0337, lift **0.97** (worse than random)
- walk-forward: lift **3.09** (21 feat) / **12.93** (+C/D)

**Also computed** (all at an alert budget, per arm): precision, transaction-level recall,
**incident recall** with Wilson 95% CIs, fraud-value recall, false alerts, false alerts per
incident caught, per-fraud-type breakdown. Headline retrained-model numbers at 100/day:
incident recall **~17.5%**, precision **~15.6%** (21 feat).

**On budget:** the single-split eval defaults to the shipped share of traffic (~15 alerts/day),
which is far below the ~98.5 fraud rows/day, so **row recall is budget-capped** — incident
recall is the fair figure. A 100-alerts/day variant exists (`ieee_train_budget100.*`) and the
benchmark uses 100/day.

---

## 6. The trained model file

- **File:** `ml/artifacts/ieee-gbm-20260917.1351-ieee.joblib` (918 KB), + report
  `ml/artifacts/evaluation-ieee-gbm-20260917.1351-ieee.json`
- **Format:** joblib pickle
- **Algorithm:** scikit-learn `HistGradientBoostingClassifier`
  (`max_iter=250, learning_rate=0.08, max_leaf_nodes=31, min_samples_leaf=40,
  l2_regularization=1.0, random_state=20260909`) wrapped in the custom
  `TimeSplitCalibratedBooster` (isotonic calibration on a time-held-out slice, no class
  weighting).
- **Library versions (`.venv`):** scikit-learn **1.9.0**, numpy **2.5.3**, pandas **3.0.5**,
  joblib **1.6.0**, Python 3.14.
- **Loading requirement:** the pickle references
  `backend/riskradar/model/calibrated.py::TimeSplitCalibratedBooster`, so `backend/` must be on
  `sys.path` to `joblib.load` it. It expects the **21-column** feature matrix in
  `MODEL_FEATURE_NAMES` order.
- Trained on: every labelled IEEE train row (590,540 rows, 20,663 fraud). Deliberately named
  `ieee-gbm-*` (not `riskradar-gbm-*`) so the demo builder never auto-registers it.

---

## 7. Assumptions, shortcuts, and known issues

- **`TransactionDT` origin is a guess.** `2017-11-30` is assumed as t=0 ("the common reading").
  Only inter-transaction gaps feed the measurements, so absolute dates don't affect metrics —
  but any calendar-based feature would be built on a guessed epoch.
- **No card number in the dataset → identity is a reconstructed proxy.** Customer/card keys are
  `card1 + addr1 + P_emaildomain + card_start_day`, and `card_start_day` is itself derived
  (`TransactionDT//86400 − D1`). Documented reason: `card1–card5` name a card *range*, so without
  this the burst rule fired ~20×/day, all false.
- **~2 of 21 model features actually vary on IEEE** (amount, hour-of-day). The rest are
  constant/not-applicable — IEEE lacks history depth, events, credits, beneficiary, and
  auth/decline data. Treat the IEEE model as an amount+hour model.
- **Rules essentially cannot fire.** 11 of 13 rules are structurally inapplicable; the burst rule
  fired 1,219 times on the test split, **every one a false alert**.
- **V1–V339 excluded by policy, never tested** (D10f).
- **C/D "ceiling" jump (3× → 13× lift) was never leakage-audited** — accepted as legitimate signal.
- **Kaggle test split has no labels** — scored/replayed only, never measured.
- **Thin behavioural history:** 73,407 reconstructed customers, half with ≤2 transactions.
- **Scope statement (from the model's own report):** *"Not a production model for a Nigerian bank:
  it learned one US e-commerce merchant's customers."* Per D81d, IEEE settles the *claim* that the
  pipeline finds real signal on real labels; it does **not** transfer detection numbers, and a
  model must be trained on each institution's own outcomes.
- **Open gap (D81e):** closing the distance to the C/D ceiling needs card-oriented features and a
  new feature spec, then retrain + re-measure — this is the natural next step for whoever takes over.

---

## How to reproduce

```bash
# 1. Join (needs the 4 raw Kaggle CSVs in ml/data/public/)
python ml/datasets/join_ieee.py

# 2. Single time-split evaluation (8 arms)
python ml/evaluate_dataset.py run ml/data/public/ieee_train_joined.csv \
    --mapping ml/datasets/ieee_cis.mapping.yaml

# 3. Walk-forward benchmark (the ROC-AUC numbers)
python ml/datasets/ieee_benchmark.py

# 4. Train the exported model on all labelled rows
python ml/datasets/ieee_model.py
```

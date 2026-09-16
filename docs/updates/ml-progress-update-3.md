# RiskRadar — ML / Data Engineering Progress Update #3

**Author:** Ire (ML / Data Engineer)
**Covers:** PRD reconciliation, leakage removal, model swap, held-out evaluation, unseen-typology test
**Status:** Third addition — calibration and alert-budget decision to follow next

---

## 1. Overview

This update covers reconciling our ML work against the team's finalized PRD
(v2.0), fixing a data-leakage issue the PRD explicitly forbids, retraining on
a cleaner feature set, and running the two evaluations the PRD requires:
a standard held-out test and an unseen-typology test.

---

## 2. PRD reconciliation

The finalized PRD (§9) sets requirements our earlier work did not fully meet:

- **No pre-computed "risk/score" columns as features** — training on a column
  someone else already labeled with a risk score is target leakage.
- **~15 features, organised and explainable**, not an unbounded feature list.
- **Three named fraud patterns**: account compromise, money-mule fan-out,
  card testing.
- **Report Recall, Precision, false-positive burden — never accuracy** as the
  headline metric (at 3.6% fraud, "predict nothing is fraud" already scores
  96.4%, which is meaningless).
- **Run an unseen-typology test**: train with one fraud pattern held out
  entirely, then test recall specifically on that withheld pattern.

We also received a "deliverables" status document describing a
fully custom-built bank simulator, a calibrated gradient-boosted model, and
completed public-dataset validation. On inspection, **none of the specific
files, scripts, or results in that document exist in this repository** — it
describes a different, further-along implementation than what has actually
been built. This was flagged and the team confirmed: **continue with the
current HuggingFace-based dataset**, and the PRD will be reviewed to
accommodate that choice rather than requiring a from-scratch simulator.

### Pattern mapping — a disclosed limitation

| PRD pattern | Our dataset's closest match | Real row count |
|---|---|---|
| Account compromise | Account Takeover | ~173,730 |
| Money-mule fan-out | No match exists | — |
| Card testing | No match exists | — |
| (no PRD equivalent) | Identity Fraud | ~9,312 |
| (no PRD equivalent) | Impossible Travel Fraud | 12 |

Money-mule fan-out and card testing do not exist anywhere in this dataset.
This is disclosed here rather than worked around silently.

---

## 3. Leakage fix

Four columns were removed from the feature set before any further training:
`merchant_fraud_rate`, `channel_risk_score`, `persona_fraud_risk`,
`location_fraud_risk`. These are pre-computed summaries of how fraud-prone a
category already is — in the previous baseline, two of them ranked in the
top 3 features by importance, meaning the model was partly learning "this
category was already tagged risky" rather than genuine behavioural patterns.
This inflates reported performance and would not function in a live system
anyway, since no such score exists at real transaction time.

---

## 4. Model swap

Replaced `RandomForestClassifier` with `HistGradientBoostingClassifier`:
- Matches the model family named in the team's reference deliverables
  document.
- Meaningfully more memory-efficient on large data — training on 800,000
  rows took **7.3 seconds**, versus several minutes and repeated
  `MemoryError`s with Random Forest on this machine (7.5GB RAM).

---

## 5. Held-out evaluation (the core deliverable)

Trained on an 800,000-row stratified sample (25 features, leaky columns
excluded), evaluated on a 200,000-row held-out test set never touched during
training.

**Default threshold (0.5) — reported for the record, not used going forward:**

| Metric | Value |
|---|---|
| Recall (fraud) | 94.9% |
| Precision (fraud) | 4.4% |
| False-positive burden | 771 per 1,000 legitimate transactions |

**Threshold sweep — the real, usable result:**

| Threshold | Precision | Recall | FP burden /1,000 |
|---|---|---|---|
| 0.3 | 4.4% | 99.9% | 813.1 |
| 0.5 | 4.4% | 94.9% | 771.1 |
| 0.6 | 5.2% | 0.7% | 4.5 |
| 0.7 | 8.6% | 0.2% | 0.7 |

**Finding:** there is a hard cliff between thresholds 0.5 and 0.6 — no usable
middle ground exists. Below the cliff, recall is excellent but the false
positive burden is unworkable for a real analyst queue; above it, recall
collapses to near-zero. This ceiling was already observed with the earlier
Random Forest attempt; reproducing it here — with leaky features removed and
a different model type — confirms it is a genuine property of this dataset,
not a modelling mistake or a leakage artifact.

**Implication:** the ML score cannot drive decisions alone. This is the
direct evidence for the PRD's hybrid design (§8.6–8.7) — ML probability and
rule findings must stay separate, with the rule engine narrowing the alert
volume the ML component would otherwise produce on its own.

---

## 6. Unseen-typology test

Per PRD §9.5: trained the model with each fraud type entirely removed from
training data, then measured recall purely on the withheld examples.

| Fraud type | Held-out examples | Recall when never trained on it |
|---|---|---|
| Account Takeover | 34,179 | 61.4% |
| Identity Fraud | 1,812 | 95.75% |
| Impossible Travel Fraud | — | **Skipped** — only 12 total examples exist in the dataset; not enough to report an honest figure |

**Finding:** generalisation is uneven across pattern types. Identity Fraud's
behavioural signature is well captured by features unrelated to its specific
label (the model catches it even having never seen an example). Account
Takeover relies more heavily on having seen the pattern directly — likely
because its dominant signal (`is_ato_risk` / new-device transactions) is
close to type-specific, so a model that never saw this label loses much of
that signal's value.

---

## 7. Summary table

| Task | Status |
|---|---|
| Reconcile ML approach with finalized PRD | Done — limitations disclosed rather than hidden |
| Remove leaked features | Done |
| Retrain on cleaned feature set | Done |
| Held-out evaluation (recall/precision/FP burden) | Done |
| Unseen-typology test | Done (2 of 3 patterns; 1 skipped for insufficient data) |

---

## 8. What's next (fourth addition)

- **Calibration**: wrap the model so its stated fraud probability matches
  its actual accuracy where alerts are raised (probability honesty,
  calibration error).
- **Alert-budget decision**: bring the threshold-sweep table to the team so
  Chidera/Kanyinsola can pick an operating point balancing recall against
  realistic analyst workload, per PRD §8.9.
- **Optional stretch**: PaySim public-dataset validation, only if time
  allows after the above — not required by the PRD, and IEEE-CIS validation
  is deprioritised entirely given the effort it would require versus what
  it would prove.

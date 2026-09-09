# RiskRadar — ML / Data Engineering Progress Update #1

**Author:** Ire (ML / Data Engineer)
**Covers:** Dataset selection, repository setup, and geographic + rule-based feature engineering
**Status:** First addition — baseline model training will follow as a second update

---

## 1. Overview

This update covers everything done on the ML/Data side before model training begins:
choosing and validating the dataset, setting up the shared GitHub repository, and
building out the fraud-detection rule logic that the model will be trained alongside.

The approach throughout has been: **don't assume a rule works — test it against
the real data first, and only fill gaps with clearly-labeled synthetic data where
the real data genuinely doesn't cover something.**

---

## 2. Dataset

**Source:** [Nigerian Financial Transactions and Fraud Detection Dataset](https://huggingface.co/datasets/electricsheepafrica/Nigerian-Financial-Transactions-and-Fraud-Detection-Dataset) (HuggingFace)

- 5,000,000 transactions, 45 original columns
- Includes `is_fraud` (boolean) and `fraud_type` (labeled: Account Takeover, Identity
  Fraud, Impossible Travel Fraud, or none)
- Already includes many pre-engineered fraud-relevant fields: spending deviation
  score, velocity score, geo-anomaly score, device/IP sharing flags, and more —
  more feature-rich than a typical raw transaction log

**Real fraud rate:** 3.59% (179,553 fraudulent out of 5,000,000 rows)

---

## 3. Repository Setup

- Private GitHub repo created: `riskradar` (team collaborators invited)
- Local project (`riskradar-ml`) structured as:
  ```
  notebooks/    — EDA, feature engineering, model training notebooks
  data/         — raw/ and processed/ (excluded from git — too large to commit)
  models/       — trained model files (excluded from git)
  simulator/    — synthetic transaction generator, incl. rare-case demo scenarios
  docs/         — this document and future updates
  ```
- `.gitignore` configured to exclude datasets, model files, and the virtual
  environment, so the repo stays lightweight for all collaborators

---

## 4. The Seven Fraud Rules — What We Found

The team defined 7 candidate fraud-detection rules at the start. Each one was
tested against the real dataset before being accepted, adjusted, or dropped —
rather than assuming they'd all work as originally written.

| # | Rule | Outcome |
|---|------|---------|
| 1 | Geographic Risk Flagging | ✅ Kept — required real-world research + synthetic augmentation (see §5) |
| 2 | Behavioral Spending Deviation | ✅ Kept — already present in dataset (`spending_deviation_score`, `user_avg_txn_amt`, `user_std_txn_amt`) |
| 3 | Temporal Anomalies (Odd Hours) | ✅ Kept — already present in dataset (`txn_hour`, `is_night_txn`) |
| 4 | Transaction Velocity | ✅ Kept — already present in dataset (`velocity_score`, `txn_count_last_1h/24h`) |
| 5 | Device & Account Takeover (ATO) | ✅ Kept, simplified — see §6 |
| 6 | Impossible Travel | ✅ Kept, but rare in real data — see §7 |
| 7 | Anomalous Airtime Top-ups | ❌ **Dropped** — see §8 |

---

## 5. Rule 1 — Geographic Risk: Research + Augmentation

**Problem found:** the real dataset only contains transactions from 10 Nigerian
cities (mapping to 10 states). None of our team's originally identified high-risk
states — Zamfara, Borno, Nasarawa, Plateau — appeared anywhere in the real data.

**Research done:** looked up real Nigerian fraud statistics to ground our
approach in fact rather than guesswork. Confirmed via NIBSS-linked reporting that:
- Lagos accounts for ~63% of national fraud volume (matches real data)
- FCT (Abuja), Ogun, Rivers, and Delta follow, in that order
- Within Lagos specifically, Ikeja, Lagos Island, and Lekki are the top hotspots —
  Lekki in particular is repeatedly named as a hub for internet/financial fraud

**What we built:**
- Mapped all 10 real cities to their correct states
- Broke Lagos down into 7 real LGAs (Ikeja, Lagos Island, Lekki, Oshodi, Ikorodu,
  Apapa, Victoria Island), weighted to match real-world hotspot concentration,
  with Lekki specifically flagged as high-risk (`is_high_risk_lga`)
- Generated synthetic transactions for the 27 missing states (all 36 states + FCT
  are now represented), with **volume weighted to match real fraud statistics**
  (Ogun, Rivers, Delta got proportionally more synthetic rows than other missing
  states, matching their real reported share)
- Every synthetic row is tagged `source = 'synthetic'` so it can always be
  distinguished from real data in evaluation
- Synthetic rows were built by **resampling real transaction patterns** (amount,
  hour, velocity, device behavior) and only relabeling the state — not inventing
  arbitrary "obviously fraudulent" values. This avoids the model trivially
  learning "state X = fraud" instead of real fraud behavior.

**Result:** high-risk states show a real, non-trivial fraud rate lift (7.17% vs
3.59% baseline) — a usable signal without being an artificial giveaway.

**Documented limitation:** rows for states with no real data are synthetic and
should be reported as such; model performance should be evaluated separately on
real vs. synthetic data to avoid overstating accuracy.

---

## 6. Rule 5 — Account Takeover (ATO)

**Original rule** required: new device + within 5 minutes of a login/password
reset + high-value transfer out.

**Problem found:** the dataset has no login-event data at all — only transaction
timestamps. Testing the full rule (using `time_since_last_transaction` as a
stand-in for "time since login") caught only **0.18%** of real labeled ATO cases —
essentially useless.

**Diagnosis:** tested each candidate field individually against the real labeled
ATO cases. Found that `new_device_transaction` alone perfectly identifies
**100%** of real ATO cases — and every single fraud case tied to this label has
this field set to `True` (fraud rate: 0% when `False`, 4.39% when `True`).

**Decision:** simplified Rule 5 to just `new_device_transaction == True`. Adding
transaction-type or amount conditions on top only hurt recall (down to 1.95%),
confirming those extra conditions weren't supported by the actual data.

**Documented limitation:** this dataset ties ATO almost entirely to device
novelty; this is a strong signal *in this dataset* but may not generalize to
real-world behavior, where legitimate customers also use new devices often.

---

## 7. Rule 6 — Impossible Travel

**Found:** the dataset already includes a pre-built `geospatial_velocity_anomaly`
flag. Validated it against the real `'Impossible Travel Fraud'` label:
- **100% recall** — catches every real labeled case
- Low precision (3%) — but only because there are just **12 real labeled cases**
  in the entire 5,086,000-row dataset. This is expected at that sample size, not
  a flaw in the rule.

**Decision:** kept as-is. With only 12 real examples, no further tuning would be
meaningful — the rule is a reliable supplementary signal for the model, not a
standalone high-confidence detector.

**Demo consideration:** because only 12 real cases exist, this is highly unlikely
to appear live during a demo. Built a hand-crafted scenario generator
(`generate_impossible_travel_scenario()` in `simulator/generate.py`) to reliably
trigger this rule on demand for presentations — clearly separate from the
training data.

---

## 8. Rule 7 — Anomalous Airtime Top-ups (Dropped)

**Problem found:** unlike the other rules, this one had **no real support at
all** in the dataset:
- Fraud rate for airtime/data transactions (3.54%) was not elevated vs. the
  overall rate (3.60%) — if anything, slightly lower
- Amount distributions for fraudulent vs. non-fraudulent airtime transactions
  were statistically indistinguishable
- The amounts themselves were unrealistic for airtime purchases — the dataset's
  "airtime" transactions averaged ₦743,673, when real MTN/Airtel top-ups are
  typically ₦100–₦20,000. The category label exists, but the dataset doesn't
  actually model real airtime economics distinctly from other transaction types.

**Decision: Rule 7 is dropped from the trained model.** There is no real data to
train or validate it against, and building it would mean the model learns
nothing genuine — only what we personally invented. Per our team's data-honesty
standard (documented in this project from the start), we do not present a rule
as data-driven when it isn't.

A lightweight version remains available only as a **scripted simulator demo**
(`generate_airtime_fraud_scenario()`) for illustrative/presentation purposes, but
it will **not** be part of the trained model or evaluated as a real detection
capability.

---

## 9. Summary of Final Rule Set Going Into Model Training

| Rule | Data-driven? | Confidence |
|------|--------------|------------|
| 1. Geographic Risk | Real + documented synthetic augmentation | High |
| 2. Spending Deviation | Fully real | High |
| 3. Odd Hours | Fully real | High |
| 4. Velocity | Fully real | High |
| 5. Account Takeover (simplified) | Fully real | High |
| 6. Impossible Travel | Fully real (very small sample) | Medium — small sample size |
| 7. Airtime Top-ups | **Dropped — no real support** | N/A |

---

## 10. What's Next (Second Addition)

The next update will cover:
- Saving the final cleaned/augmented dataset (`data/processed/nigerian_transactions_clean.csv`)
- Baseline model training (Random Forest, `class_weight='balanced'`)
- Model evaluation — **reported separately for real vs. synthetic data**, per the
  data-honesty approach established in this document
- Feature importance review, to confirm the model isn't over-relying on any
  single field (e.g. checking it isn't just keying off `new_device_transaction`)

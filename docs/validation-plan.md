# Risk Radar — Validation Plan

**Why this document exists:** everything else in this project grades itself. We
wrote the simulator and we wrote the detector. This sets out why that is not
validation, what would be, and how far up that ladder we have actually climbed.

---

## 1. Why simulator results are not validation

### 1.1 The circularity is structural

The generator/detector wall (D10a) stops *code* leaking between the two sides.
It cannot stop the fact that both encode **the same person's model of what
fraud looks like**. Agreement between them measures the internal consistency of
our beliefs, not their correspondence to reality.

Formally, the evaluation estimates `P(detect | fraud as we imagine it)`, not
`P(detect | fraud)`. The test set is drawn from the distribution the generator
defines. There is no independent sample anywhere in the experiment.

Held-out typology (D10b) genuinely helps — it tests generalisation to variants
never trained on. But *we invented all three typologies*. It measures "can this
generalise across my imagination", which is a real property and a lower bound on
nothing external.

### 1.2 The negative class is the bigger problem

Attention naturally goes to whether the *fraud* is realistic. The harder
question is whether **normal** is realistic.

Our normal is a smooth parametric draw. Real normal is lumpy, seasonal, and full
of legitimate behaviour that looks exactly criminal:

- A funeral, where thirty relatives send money into one account — structurally
  identical to mule fan-in
- Salary day, school-fees season, Christmas
- A market trader making forty transfers a day, every day
- Someone who has just sold a car

This matters more than it first appears, because **threshold derivation is
driven entirely by the right tail of the negative distribution**. The alert
budget arithmetic — the operational heart of this system — is calibrated against
a shape we invented.

> The 0.92 incident recall is not the fragile number. **The alert volume is.**

### 1.3 Calibration is a property of a distribution

We claim isotonic-calibrated probabilities. Move to real data — different base
rate, different feature distributions — and the calibration is void. That
destroys the one thing calibration buys us: predicting alert volume at a
threshold.

### 1.4 Feature semantics do not transfer

`device_is_new_to_subject` means "the simulator drew a new string". In
production it means whatever the bank's fingerprinting library computes, which
flips on app updates, OS upgrades, cleared caches and a new phone at Christmas.
Its base rate could be 30% rather than 3%, and the model has learned a
coefficient for a variable that means something else.

### 1.5 The hardest production problem is entirely absent

Our labels are perfect by construction. Real ones are **delayed** (chargebacks
arrive 30–120 days later), **noisy**, and **biased by our own decisions** — you
only ever learn the outcome of cases you alerted on. Everything below threshold
is unlabelled and silently treated as legitimate. Retrain on that and the model
reinforces its own blind spots.

This is the *selective labels* problem, and it is the single hardest thing about
production fraud ML. We have never had to confront it.

### 1.6 There is no adversary

Real fraud adapts to detection. Our typologies are static. We have no evidence
about the only regime that ultimately matters.

---

## 2. The ladder

| Rung | What it is | What it settles | Status |
|---|---|---|---|
| 0 | Simulator + held-out typology | Methodology, pipeline correctness, no leakage | **Done** |
| 1a | **PaySim** | Pipeline runs on foreign data; threshold discipline at a real base rate | **Done — §3** |
| 1b | **IEEE-CIS** | Imbalance, calibration and thresholds against **real chargeback labels** | **Blocked — §4** |
| 2 | Realism audit of the negative class | Whether our "normal" resembles Nigerian retail banking | Not started |
| 3 | **Shadow deployment** | Whether it actually works | Needs a bank |
| 4 | Holdout population | Attributable prevented loss | Needs a bank |

Only rung 3 settles the question. Rungs 1 and 2 make the claim defensible in the
meantime; rung 4 is what makes the business case measurable rather than asserted.

---

## 3. Rung 1a — PaySim, completed

`python ml/validate_public.py --dataset paysim` · results in
`ml/artifacts/public-validation-paysim.json`

1.2 million transactions, 0.127% fraud, run through **our own feature package**
(`riskradar.features`) with a temporal split and the same calibrated model.

| Measure | Result |
|---|---|
| PR-AUC | **0.169** against a random baseline of 0.0034 — ~50× chance |
| ROC-AUC | 0.815 |
| Precision at the alert budget | 0.357 |
| Transaction recall at the budget | 0.178 |
| **Value-weighted recall** | **0.653** |

### 3.1 What the value metric revealed

The model catches **65% of the money while catching 18% of the transactions**.
That is the right failure mode — it is finding the large frauds and missing the
small ones — and count-recall alone would have hidden it completely. This is
precisely why `value_weighted_recall` was added.

### 3.2 A limitation of PaySim that is rarely stated

We measured it rather than assuming it:

| | |
|---|---|
| Transactions per **sender** | mean **1.001**, max **3** |
| Transactions per **destination** | mean 2.34, max 113 |

**PaySim has identities but not behaviour.** Its accounts almost never appear
twice, so every sender-side behavioural feature is structurally inert.
`validate_public.py` detects this automatically and names the dead features:

```
inert_features: failed_attempts_1h_account, decline_rate_24h_account,
                beneficiary_is_new_to_account, device_is_new_to_subject,
                account_age_days, days_since_account_activity
```

**Six of our twelve features cannot fire on PaySim.** So it validates that the
pipeline runs on foreign data and that threshold discipline holds at a realistic
base rate — and it does **not** validate the behavioural features that are this
system's actual claim. Papers that use PaySim to validate velocity features are
validating something that is not there.

---

## 4. Rung 1b — IEEE-CIS, blocked

The only rung short of shadow deployment that carries **real fraud labels**:
~590,000 e-commerce transactions with genuine chargeback outcomes, ~3.5%
positive.

**Status: blocked at account verification.** The dataset sits behind Kaggle
competition rules. Accepting those rules requires **phone-number verification
with a CAPTCHA**, which is a step that must be completed by a person.

`ml/validate_public.py --dataset ieee` is written and ready. To unblock:

1. Sign in to Kaggle and open
   `https://www.kaggle.com/competitions/ieee-fraud-detection/rules`
2. Complete phone verification, then accept the competition rules
3. Download and unzip `train_transaction.csv` into `ml/data/public/`
4. `python ml/validate_public.py --dataset ieee`

Its features are anonymised (`V1`…`V339`), so our feature package cannot run on
it and the script uses the dataset's native columns instead. That is deliberate:
IEEE-CIS validates **methodology** — temporal splitting, imbalance handling,
calibration quality, threshold derivation — against ground truth somebody else
produced. It does not validate our feature semantics, and pretending otherwise
would be exactly the self-deception this document exists to prevent.

---

## 5. Rung 2 — auditing the negative class

Not started, and cheap. Take published aggregates — NIBSS annual fraud reports,
CBN payment system statistics — and compare our simulator's marginals against
them: channel mix, average transfer value, NIP versus intrabank share, fraud
loss by channel.

This is not validation. It converts "we made it up" into "we made it up and then
checked the shape", and it is the only rung that addresses §1.2, which is the
most serious of the objections above.

---

## 6. Rung 3 — shadow deployment

The only genuine answer. Run in parallel on real traffic, scoring everything,
alerting nobody, for four to eight weeks. Then:

- Compare our alerts against the incumbent rule engine's — agreement, and more
  importantly disagreement
- Have analysts review a stratified sample of what we would have caught that the
  incumbent missed
- **Measure actual alert volume against the budget.** This is where the
  threshold arithmetic is genuinely tested for the first time
- Wait for chargeback and confirmed-fraud labels to mature, then compute recall

## 7. Rung 4 — a holdout population

Without a control group, prevented loss can never be attributed. Randomised
allocation where the regulator permits it, or at minimum a population the model
does not score, so the counterfactual exists.

---

## 8. Metrics, and why these ones

| Metric | Why it is here |
|---|---|
| **PR-AUC** | ROC-AUC flatters imbalanced problems; the false-positive rate barely moves when negatives dominate |
| **Recall at a fixed alert budget** | Not recall at 0.5. Operations dictate the operating point |
| **Incident-level recall** | One alert opens the case and the analyst then sees the whole timeline (D24a) |
| **Value-weighted recall** | A desk is judged on money, not counts. PaySim showed why: 0.65 value against 0.18 count |
| **Time to detection** | Catching a fan-out on transaction 2 versus transaction 11 is the difference between recovery and write-off. Reported with *value still preventable at first alert* |
| **False-positive rate by segment** | A model that alerts disproportionately on USSD alerts disproportionately on the poorest customers. One global rate hides that entirely |
| **Reliability curve** | Calibration is the claim that makes volume predictable; the curve is how it is checked rather than asserted |
| **PSI on features and scores** | Drift. Schema supports it; meaningful once a second model exists |
| ~~Accuracy~~ | **Banned** (D10c). At a 0.3% base rate, predicting "never fraud" scores 99.7% |

---

## 9. What we are entitled to claim today

> We cannot claim this system detects fraud.
>
> We can claim, and prove, four things: the methodology is free of the leakage
> traps that make most fraud-ML numbers meaningless; the pipeline is correct end
> to end and runs on data we did not generate; the three-layer architecture
> measurably beats the model alone on unseen typologies, 0.42 → 0.92; and the
> ladder above is the exact route from that position to a real claim, with
> shadow deployment as the only rung that settles it.

That is a stronger position than an inflated number, and it is the one an
examiner who knows the field will respect.

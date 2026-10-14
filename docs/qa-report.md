# Risk Radar — QA and Results Report

**Date:** 2026-09-09 · **Version:** 1.0 · **Owner:** Kanyinsola (PM & Documentation Lead)

Every figure here was produced by running the system. The unflattering ones are
included on purpose — PRD §12.3 commits to publishing the honest number rather
than the flattering one, and a report that only carries good news would break
that commitment on its first page.

---

## 1. Test coverage

**54 tests, all passing**, run against a real PostgreSQL 16 instance. Not a mock
and not SQLite: `SELECT … FOR UPDATE SKIP LOCKED`, `LISTEN`/`NOTIFY` and the
`audit_log` grant have no meaning on any other engine, so a suite that avoided
Postgres would be testing a different system than the one that ships.

| Suite | Covers |
|---|---|
| `test_train_serve_equality.py` | **The highest-value test (D15).** Identical feature vectors from the SQL path, the naive pandas path and the indexed training path, on a deliberately awkward fixture — declines, reversals, a null beneficiary, a window-boundary row, an out-of-window row |
| `test_ingestion.py` | API-key auth, idempotent resubmission, persist-and-enqueue atomicity, tokenisation (asserting no raw identifier survives anywhere in the row), dead-lettering of six malformed payload shapes, replay defaults |
| `test_policy_and_rules.py` | Band derivation, escalate/suppress/override ordering, suppression cancelling an escalation, override beating suppression, rule-only mode not inventing a probability, signals carrying no points |
| `test_security.py` | Separation of duties per role *and* over HTTP, MFA enforcement, login not leaking account existence, immediate revocation, the `audit_log` grant, hash-chain verification, the generator/detector import wall |
| `test_worker_concurrency.py` | Lease disjointness across workers, idempotent re-scoring, a decision written even when no alert is raised |

Two properties are enforced by **structure** rather than by test, because no test
could see them:

- `riskradar.features` never reads the mutable `accounts` table. A test that
  compared two code paths would pass happily while both were point-in-time
  leaked, so the account context is stamped at the boundary instead (D22b).
- The application role cannot `UPDATE` or `DELETE` `audit_log`. That is a
  database grant; the test verifies it, but the grant is the control.

---

## 2. Detection results

### 2.1 Corpus

| | |
|---|---|
| Transactions | 601,869 over 29.8 days |
| Fraudulent | 1,869 (**0.311%**) |
| Incidents | 150 — 45 account takeover, 48 mule fan-out, 57 card testing |
| Alert budget | 120/day (3 analysts × 40 reviewable alerts) |

### 2.2 Held-out typology — the published figure

Trained on two typologies, evaluated on a third never seen in training (D10b).

| Held out | Model alone | Model + rules + policy | Incidents |
|---|---|---|---|
| Account takeover | 0.733 | **0.800** | 45 |
| Mule fan-out | 0.521 | **1.000** | 48 |
| Card testing | **0.000** | **0.947** | 57 |
| **Mean incident recall** | **0.418** | **0.916** | 150 |

**The card-testing row is the finding.** A model can only recognise shapes
resembling something it was trained on; a deterministic rule has no such limit.
That gap — 0.000 to 0.947 — is the measured argument for keeping rules as a
separate layer emitting named facts (D11), rather than folding them into the
model as features or blending them into its score.

### 2.3 The numbers that flatter, and why they are not the headline

| Figure | Value | Why it is not the headline |
|---|---|---|
| In-distribution PR-AUC | 0.946 | Measures the model recognising fraud shaped like fraud it trained on |
| In-distribution incident recall | 1.000 | Same |
| Transaction-level recall at budget | ~0.10 | A misleading denominator — see below |

**Why recall is counted per incident.** Correlation means one alert opens the
case, and case detail then puts the subject's *whole* timeline in front of the
analyst, alerted and un-alerted alike (FR-024, D24b). Catching 1 of an incident's
11 transactions catches the incident. Both numbers are published together with
this explanation so the low one is volunteered rather than discovered (D24a).

### 2.4 The cost of that recall

On the card-testing hold-out the system raised **1,491 alerts against an 894
budget** — 1.7×. The rules layer bought its recall with alert volume. That is
unfinished work and exactly the tuning conversation the alert budget exists to
force; it is not a solved problem.

---

## 3. Performance

### NFR-001 — sustained, `PASS`

| | |
|---|---|
| Ingested | 3,000 at 50.2 TPS |
| Lost | **0** |
| End-to-end p50 | 310 ms |
| **End-to-end p95** | **878 ms** (target < 2,000 ms) |
| End-to-end max | 1,692 ms |

End-to-end means `decided_at − ingested_at`: queue wait counts. The worker's own
`latency_ms` measures only the scoring work (p50 14 ms, p95 23 ms) and would
flatter the result by excluding exactly the thing a queue can get wrong.

### NFR-002 — burst, `PASS`

| | |
|---|---|
| Ingested | 15,000 at 250.9 TPS for 60 s |
| Queue peak | 11,992 rows |
| Lost | **0** |
| Backlog cleared | 171.3 s at 64.9 TPS (three workers) |

Latency during the burst reaches minutes. **That is the design working, not
failing.** NFR-002 makes no latency claim on purpose: absorbing a 5× burst in a
queue *means* latency grows. The system is advisory and not in the money path, so
the correct behaviour under overload is a slower decision, never a dropped
transaction (NFR-004).

### 3.1 Two performance defects found and fixed

**180 ms → 14 ms per decision.** First measurement was 6.6 TPS, nowhere near
NFR-001. Two causes: the ablation explainer made thirteen separate single-row
predictions per transaction (batched into one matrix), and the ruleset and
threshold set were re-read from the database on every transaction (hoisted to
once per batch). Attributions are now computed only for decisions that become
alerts — the requirement is explanations on alerts, and the feature snapshot
makes any other attribution recomputable exactly.

**Three workers that were not faster than one.** Adding two workers moved burst
drain time from 239 s to 241 s. `pg_advisory_xact_lock`, used to serialise the
audit chain, lives until the *transaction* ends — and the transaction spanned a
64-transaction batch, so the first alert in a batch held a global lock for the
rest of it. Workers serialised, then deadlocked; a deadlock rolled the
transaction back, releasing the claim on unprocessed rows while the loop kept
going, producing 77 duplicate-key violations in one run. Replaced with a
committed lease and one transaction per scored transaction: drain 171 s,
throughput 49.8 → 64.9 TPS, worker errors zero.

**Scaling is demonstrated, not characterised.** Three workers gave 1.3×, not 3×.
The remaining bottleneck is a single Postgres instance on the laptop the demo
runs on. The lock contention is gone; the ceiling has not been mapped.

---

## 4. Security verification

| Claim | How it was checked | Result |
|---|---|---|
| Application role cannot rewrite the audit log | `UPDATE`, `DELETE`, `TRUNCATE` attempted against the live database as `riskradar_app` | All three refused — `InsufficientPrivilege` |
| Audit chain detects tampering | Recomputing a row's hash after altering one field | Hash changes; `verify` reports the break and names the row |
| Audit actor is never null | `INSERT` without `actor_user_id` | `NotNullViolation` |
| ADMIN cannot reach a case | `GET /v1/cases` as ADMIN | `403` — enforced on the route, not by hiding a button |
| MFA is mandatory for privileged roles | Correct password, no TOTP code | `mfa_required`; no session issued |
| Logout revokes immediately | `/v1/auth/me` after logout | `401` |
| Login does not enumerate accounts | Unknown user vs wrong password | Identical status and message |
| Raw identifiers never persist | Full transaction row searched for the submitted `customer_id` and `account_id` | Not present anywhere in the row |
| Generator/detector wall | Import graph of `rules`, `policy`, `features`, `model` walked with `ast` | No simulator import |

---

## 5. Known limitations

1. **All data is synthetic.** The generator/detector wall is enforced and the
   simulator models processes rather than thresholds, but validating a simulator
   against itself remains the deepest limitation of this evaluation. IEEE-CIS was
   specified as the non-circular public validator (D10d) and has **not been run**.
2. **The core banking reference model is unverified.** Every Finacle specific
   comes from general public knowledge, not from an installation.
3. **The system evaluation approximates one rule gate.** The card-testing rule
   keys on `instrument == CARD`; offline it is inferred from the decline
   signature rather than replaying the original row.
4. **Two override rules and one suppress rule contribute nothing offline**,
   because the sanctions, mule and allowlist tables are empty in evaluation. In
   production they would carry weight, which means 0.916 is the pessimistic figure.
5. **Alert volume exceeds budget on one hold-out** — 1,491 against 894.
6. **Scaling measured at 1.3× on three workers**, single Postgres, one laptop.
7. **NDPA compliance is a documented position, not an assessment.** Obligations
   are not engaged in v1 because no personal data exists; a lawful-basis
   assessment and retention schedule remain preconditions of ingesting any real
   data (D28).
8. **The demo profile is deliberately fraud-rich** (~2–4%, not the 0.311%
   operating point) so the case queue has something in it (D38). Every figure in
   §2 comes from the training profile at the real base rate.

---

## 6. What would be done next

| Next | Why it is next |
|---|---|
| Bring card-testing alert volume inside budget | The only measured NFR-adjacent miss |
| Run IEEE-CIS validation | The only route out of validating a simulator against itself |
| Characterise the scaling ceiling | 1.3× is a data point, not a curve |
| Second model → shadow mode and PSI drift | Both are schema-supported and design-documented; neither is meaningful with one model |
| Fraud-ring graph analysis | The customer-level token added in D19 is the seam it needs |

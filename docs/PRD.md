# Risk Radar — Product Requirements Document

**Product:** Risk Intelligence — Real-Time Transaction Fraud Detection System
**Group:** RiskRadar
**Version:** 1.3
**Date:** 2026-09-13
**Owner:** Kanyinsola (PM & Documentation Lead)
**Status:** **Built and measured.** Scope frozen after Week 1; all open questions closed. §17 records settled parameters and the figures they were verified against. Results in [qa-report.md](qa-report.md).

---

## 1. Document control

This PRD supersedes the scope sections of the *Note of Concept* and the *Implementation Plan* wherever they conflict. Every design decision below is recorded with its rationale in [decisions.md](decisions.md).

Three specific corrections to the earlier documents are marked **[CORRECTION]** inline.

**v1.3 amendments (2026-09-13).** Risk Radar was mapped against a reusable *Enterprise Fraud Intelligence Platform* base PRD (§18, [base-prd-alignment.md](base-prd-alignment.md), decisions **D69–D69k**). Four requirements were added and built: FR-025, FR-035, FR-043, FR-044. Two base requirements are rejected for v1 because they conflict with D7 and D11. **No scope grew.**

**v1.1 amendments (2026-09-09).** Learning that the target bank runs a Finacle core banking system prompted a review of the identity model and the assumed event source. Two design defects were found and corrected — see §6.1, §6.2 and the new §9.1, recorded as decisions **D18–D20**. **No scope changed.** Supporting detail is in [core-banking-reference.md](core-banking-reference.md).

> **On the core banking reference:** the team has **no access to Finacle, to any installation, to its documentation, or to any bank's data.** The reference model is assembled from general public knowledge of Finacle's data model and of Nigerian retail banking, is **unverified against any bank's actual system**, and exists only to shape the simulator and to show the canonical schema is implementable. It is not evidence of bank engagement and does not alter D3 (no signed sponsor).

| Role | Name | Ownership |
|---|---|---|
| Project Manager & Documentation Lead | Kanyinsola | Sprint plan, risk register, test cases, acceptance sign-off |
| ML / Data Engineer | IRE | Simulator, feature package, model, evaluation |
| Backend & Database Developer | Chidera | Ingestion, queue, scoring worker, policy layer, identity, audit, schema |
| Frontend Developer & UI/UX | Paul | Dashboard, case UI, Figma, SSE client |

---

## 2. Product summary

Risk Radar ingests financial transactions in real time, scores each one using a calibrated machine-learning model combined with configurable deterministic rules, and surfaces suspicious activity to fraud analysts as explainable alerts grouped into investigable cases — with a tamper-evident audit trail behind every decision and action.

**It is advisory.** It produces a decision; it does not enforce one. It is not in the money path.

### 2.1 What it is not

- It does not identify a person as a fraudster. It identifies **activity warranting investigation**.
- It does not block, hold, reverse or freeze anything. Enforcement belongs to the caller.
- It does not hold reversible personal identifiers. Account identity is tokenised one-way at the boundary.
- It does not connect to any live banking system in v1.

---

## 3. Problem and goals

### 3.1 Problem

Static rule-based transaction monitoring fails in two directions at once. It misses fraud whose patterns move faster than manually maintained thresholds, and it generates false positives that consume analyst capacity and create customer friction. Analysts receiving a bare risk score cannot act on it: they need the reasons, the behavioural context, a controlled workflow, and a record that survives scrutiny months later.

### 3.2 Goals

| ID | Goal | How it is measured |
|---|---|---|
| G1 | Detect fraud patterns single-transaction rules cannot express | Recall on a **held-out fraud typology** never seen in training |
| G2 | Keep alert volume inside analyst capacity | Alerts per analyst per day, within the configured alert budget |
| G3 | Make every alert explainable | 100% of alerts carry named signals with evidence, plus model feature attributions |
| G4 | Make every decision defensible after the fact | 100% of decisions reproducible from stored model version, ruleset version and feature snapshot |
| G5 | Prove the system holds up under load | Published NFR met under load test, zero transaction loss |

### 3.3 Non-goals for v1

Blocking/enforcement · fraud-ring graph analysis · regulatory reporting (CBN / NIBSS / NFIU) · multi-tenancy · real bank integration · mobile app.

---

## 4. Scope

### 4.1 In scope — the spine

1. Versioned, authenticated ingestion API with idempotency, schema validation and dead-lettering
2. Postgres-backed work queue and scoring worker
3. One-way HMAC tokenisation of account identifiers **and point-in-time stamping of account context** at the ingestion boundary [v1.1]
4. Shared feature package used identically by training and serving
5. Rules layer emitting named signals — **6 rules: 2 escalate, 2 override, 2 suppress** [v1.2]
6. One calibrated supervised model
7. Policy layer combining model and signals into a decision, with audited thresholds
8. Decision to Alert to Case object model with subject-window correlation
9. Authentication, four-role RBAC with separation of duties, hash-chained audit log
10. Analyst dashboard: alert queue, case detail with explanations, case actions, aggregate charts
11. Latent-process transaction simulator covering three fraud typologies
12. Load test verifying the performance NFR

### 4.2 Deferred — designed, not built

| Item | Why deferred | What v1 must still do |
|---|---|---|
| Shadow mode / champion-challenger | Meaningless with one model and no production traffic | Schema supports it; design documented |
| PSI drift monitoring | Same | Schema supports it; design documented |
| Full model registry | Over-engineering for one model | Ship a `model_versions` table |
| PaySim validation pipeline | Two dataset mappings is one too many | IEEE-CIS only; PaySim documented as planned |
| Batch/replay implementation | The contract matters more than throughput now | Ship endpoint + schema; a naive loop is acceptable |
| Fraud-ring / graph analysis | V2 | Nothing |
| Automated response / blocking | V2 — and safe to defer *because* the system is advisory | Nothing |
| **[v1.2] Rules 8–10 → 6, features ~15 → 12** | Compensating cut paying for the identity work added in D19a/D22 | Every rule *power* in §11.2 retained — 2 escalate, 2 override, 2 suppress |
| **[v1.2] Separate INFOSEC screen** | Same | A saved filter on the existing queue. The role and its separation of duties are unchanged |
| **[v1.2] Separate metrics screen (FR-034)** | Same | Folded into the FR-032 aggregate charts |
| **[v1.2] `beneficiary_subject_token` population** | Same | Column ships; the simulator emits null in v1 |

### 4.3 Out of scope

Real bank integration · real customer data · regulatory reporting · production hosting inside a bank · SSO integration (seam only) · anything requiring a signed bank sponsor · **any core banking connector, adapter or data access — the reference model in §9.1 is design-time only** [v1.1].

---

## 5. Users and roles

Four roles, with **separation of duties enforced in code**: the account that tunes detection cannot be the account that clears what detection misses.

| Role | Can | Explicitly cannot |
|---|---|---|
| `ANALYST` | View queue; open cases; add notes; set outcome (confirmed fraud / false positive / inconclusive) | Close cases; change thresholds; manage users |
| `FRAUD_OPS_LEAD` | All analyst powers, plus close/resolve, reassign, view metrics dashboard | Change rules or thresholds |
| `INFOSEC_ANALYST` | View compromise-flagged cases; cross-subject security view **(a saved filter on the queue, not a separate screen — v1.2)** | Set commercial fraud outcomes |
| `ADMIN` | Manage users, rules, thresholds, model promotion | **Review or decide any case** |

---

## 6. Domain model

### 6.1 Canonical transaction event

**[CORRECTION]** The Implementation Plan's channel list — *Mobile, Web, POS, ATM, Card, Transfer* — conflates two orthogonal dimensions and omits USSD. A mobile-app NIP transfer is both "Mobile" and "Transfer"; a single enum cannot express that, and a model trained on it is blind to the distinction between card-not-present web activity and card-present POS activity.

```
channel     : MOBILE_APP | WEB | USSD | POS | ATM | AGENT | BRANCH | API
instrument  : CARD | ACCOUNT_TRANSFER | CASH | WALLET
rail        : NIP | NEFT | RTGS | CARD_SCHEME | INTRABANK | ATM_NETWORK
```

Required fields on every event:

| Field | Type | Notes |
|---|---|---|
| `transaction_ref` | string | Caller-supplied. Idempotency key. Unique |
| `occurred_at` | timestamptz | When it happened. **All behavioural features use this** |
| `ingested_at` | timestamptz | When we received it. Never used for velocity |
| `amount_minor` | bigint | **Kobo. Integer. Never floating point, including in aggregates** |
| `currency` | char(3) | ISO 4217 |
| `channel` / `instrument` / `rail` | enum | See above |
| `subject_token` | string | HMAC-SHA256 of **customer** identity + pepper. **The subject of a case** |
| `account_token` | string | HMAC-SHA256 of the **sender account** + pepper. **The subject of a behavioural baseline** |
| `beneficiary_token` | string | HMAC-SHA256 of beneficiary account + pepper |
| `beneficiary_subject_token` | string | Nullable. Beneficiary at customer level, intrabank only |
| `device_token` | string | Hashed device fingerprint. **Channel-layer field — see §9.1** |
| `ip_region` | string | Coarse geo. Not raw IP. **Channel-layer field** |
| `merchant_category` | string | Nullable; POS/card only. **Switch-layer field** |
| `auth_result` | enum | `APPROVED` / `DECLINED` / `FAILED` / `REVERSED`. **An event is an attempt, not a success** |
| `decline_reason` | string | Nullable. Coarse category: `INSUFFICIENT_FUNDS`, `LIMIT_EXCEEDED`, `INVALID_PIN`, `DO_NOT_HONOUR`, `TIMEOUT` |
| `display_name` | string | Obviously-synthetic label, analyst UI only |
| `bvn`, `nin` | string | **[v1.2, D75]** Optional, eleven digits. Tokenised at the boundary into `bvn_token`; when absent, resolved from the core by customer number. Never stored raw |

Core-derived account context, **stamped onto the event at the ingestion boundary** and immutable thereafter (§9.1, §6.3, D20b, D22):

| Field | Type | Notes |
|---|---|---|
| `account_opened_at` | timestamptz | **A fact, not a derived age.** `account_age_days` is derived at feature time from `occurred_at` — mule fan-out signal |
| `last_activity_at` | timestamptz | **A fact, not a status.** Dormancy band is derived at feature time — account-takeover signal |
| `product_type` | enum | `SAVINGS` / `CURRENT` / `DOMICILIARY` / `WALLET`. Segments the baseline |
| `origin_sol_id` | string | Branch / service outlet owning the account. Coarse geography without a raw IP |

**Why two timestamps:** a batch of late-arriving transactions scored on `ingested_at` manufactures a fake velocity spike and floods the alert queue. If only one timestamp is stored the information is simply gone — this is unrecoverable after the fact.

**Why integer kobo:** floating-point money is the most common production defect in financial software, and every aggregate you compute — velocity sums, customer baselines, thresholds — inherits the error.

**Why two identity levels [v1.1]:** banking identity is customer-first — one customer holds a salary current account, a savings account, sometimes a domiciliary account. Tokenising only the account means an account takeover that drains three of one victim's own accounts opens **three cases for one incident**, which is precisely the failure correlation exists to prevent. But correlating *and* baselining at customer level is equally wrong: averaging a salary account with a dormant domiciliary account produces a baseline that flags nothing and excuses everything. So the case correlates on `subject_token` and the baseline is computed on `account_token`. Both remain one-way HMACs with the pepper — §10 is unchanged, nothing became reversible.

**Why an outcome field [v1.1]:** without `auth_result` every event describes a transaction that succeeded, and three things break. **Card testing becomes unrepresentable** — §12.2 defines it as authorisation probes, and probes are overwhelmingly declines; simulating it as a run of tiny *successful* transactions is not card testing, it is a small-amount pattern, and the model would learn the wrong thing while a third of the held-out evaluation in §12.3 rests on it. **Account-takeover limit-probing becomes invisible** — failed attempts clustering against insufficient funds and daily transfer limits are among the strongest real ATO signals. **Reversals count as money moved** — a failed NIP transfer is auto-reversed, and a velocity sum blind to that counts the same naira twice, undoing §6.1's integer-kobo discipline by double-counting rather than by rounding.

Three consequences follow, and all three are cheaper now than after Week 2:

- **`riskradar.features` splits by outcome.** Every value-summing feature filters to `APPROVED`; `failed_attempts_1h` and `decline_rate_24h` become first-class features. Changing the shared package (§12.5) before the equality test exists is materially cheaper than changing it after.
- **Declines are features, not automatically alerts.** A probing burst produces far more declines than approvals; routing them straight to the alert path would blow past the alert budget in §11.3. The policy layer decides, as it does for everything else.
- **It sharpens §9.1.** "Only the switch layer sees declines" is the single clearest reason Risk Radar consumes the switch rather than the core, and the schema now says so.

**Why account context is stamped, not joined [v1.1]:** `product_type`, `origin_sol_id`, `account_opened_at` and `last_activity_at` describe an *account*, and the normalising instinct is to put them in a dimension table the feature package joins to. That instinct breaks two commitments at once. **G4 dies:** re-derive a decision from six weeks ago against a mutable account table and you read *today's* dormancy, so the decision reproduces to a different answer. **§12.5 gets worse, not better:** training joins the account table as it stands today, so an account marked dormant *after* — often *because of* — the fraud carries that attribute backwards into every training row. That is point-in-time leakage, it inflates offline metrics, and the shared feature package does **not** catch it, because both paths call the same functions and both joins are wrong in the same direction.

So the boundary stamps and the interior never looks up. A mutable `accounts` dimension still exists — the analyst UI needs it for the baseline panel in FR-024, and cross-account subject lookup needs it — but `riskradar.features` never reads it. And the stamped values are **facts with timestamps rather than derived numbers**: `account_opened_at` never changes, whereas `account_age_days` changes every night. Deriving age and dormancy from `occurred_at` at feature time is point-in-time correct *by construction*, in the pandas path and the SQL path alike, with no as-of join to get wrong.

*(Nigerian practice bands accounts as inactive and then dormant at roughly six and twelve months without customer-induced activity. Confirm the current CBN figures before quoting them; the design does not depend on the exact boundary, only on deriving it from `last_activity_at`.)*

### 6.1a Event envelope [v1.2, D72]

A payment is one event type among several. Account takeover announces itself **before** the money moves: a login from a new device, a changed PIN, a newly enrolled payee, a SIM swap, a raised transfer limit. None of these is a transaction, and §6.1 cannot express them. Every event now shares one envelope:

| Field | Type | Notes |
|---|---|---|
| `event_ref` | string | Caller-supplied idempotency key, **unique across every type**. A payment's is its `transaction_ref` |
| `event_type` | enum | `PAYMENT` / `LOGIN` / `DEVICE_BOUND` / `CREDENTIAL_CHANGED` / `PAYEE_ADDED` / `SIM_CHANGED` / `LIMIT_CHANGED` |
| `occurred_at` | timestamptz | As §6.1: every detector uses this, never `ingested_at` |
| `subject_token` | string | The customer, tokenised at the boundary |
| `account_token` | string | Nullable; required for `PAYEE_ADDED` and `LIMIT_CHANGED` |
| `device_token` | string | Nullable; required for `DEVICE_BOUND` |
| `ip_region`, `channel` | | Channel-layer fields, as §6.1 |
| `detail` | object | Type-specific facts, validated per type and tokenised (`PAYEE_ADDED` stores a `beneficiary_token`, `SIM_CHANGED` an `msisdn_token`). Empty for a payment |

**Why the payment columns did not move into `detail`:** the feature package, the model and every stored decision read `transactions` as typed columns, and §12.5's train/serve equality is proven against them. The envelope is the spine; a payment's envelope row links to its `transactions` row and is written in the same database transaction. Nothing about payment scoring changes. `POST /v1/events` accepts any type (a payment as `{event_type: PAYMENT, payment: <§6.1 event>}`); `POST /v1/transactions` stays as the payment-only door. A decision is made on a `PAYMENT` (`SCORED_EVENT_TYPES`); since feature spec 1.2.0 (D77) the other types are read as history before it: failed logins, the paying device's binding, credential and SIM changes, and the destination's enrolment, over a 72-hour window.

### 6.2 Decision, Alert, Case

**[CORRECTION]** The Note of Concept, the MVP table and the user stories use *alert* and *case* interchangeably across three different state vocabularies (four states, six verbs, and outcomes applied to alerts rather than cases). They are different objects and the difference is load-bearing.

```
TRANSACTION --scored--> DECISION --if actionable--> ALERT --correlated--> CASE
                       (immutable)                 (immutable)           (mutable)
```

- **Decision** — one per scored transaction, always written, never mutated. Carries score, band, `model_version`, `ruleset_version`, `threshold_version` and the full feature snapshot. This is what makes a decision reproducible months later.
- **Alert** — raised when a decision crosses the actionable threshold. An observation; observations do not change their minds.
- **Case** — the investigative container. State, assignee, notes, outcome. **Holds many alerts.**

**Correlation rule [v1.1]:** a new alert joins an existing **open** case sharing the same **subject** — `subject_token`, **the customer, not the account** — within a **correlation window** of default 24 hours, stored as configuration. Otherwise it opens a new case.

*Why:* an account takeover producing reconnaissance, a device change and six rapid transfers generates eight alerts. Without correlation that is eight cases for one incident, investigated eight times, and every queue metric becomes fiction. And if the attacker moves through the victim's savings account as well as their current account, an account-level subject reproduces the same fragmentation one level up.

### 6.3 Case state machine

```
OPEN --> UNDER_REVIEW --> { CONFIRMED_FRAUD | FALSE_POSITIVE | INCONCLUSIVE } --> CLOSED
              |
              +--> ESCALATED --> (INFOSEC | FRAUD_OPS) --> back to review, or CLOSED
```

`INCONCLUSIVE` is mandatory. Forcing a binary under time pressure produces analysts who pick whichever option is faster, poisoning training labels with false certainty.

**Case outcomes are a first-class ML training signal, not a UI status.** `CONFIRMED_FRAUD` and `FALSE_POSITIVE` are analyst-labelled ground truth — the only non-circular labels this system will ever generate, and the eventual exit from simulator-only training.

---

## 7. Functional requirements

### 7.1 Ingestion

| ID | Requirement | Priority |
|---|---|---|
| FR-001 | `POST /v1/transactions` accepts a single transaction, authenticated by API key | Must |
| FR-002 | `transaction_ref` is an idempotency key; a repeat submission is a no-op returning the original result | Must |
| FR-003 | Payloads failing schema validation are written to a dead-letter table with the raw body and the validation error. **Never silently coerced** | Must |
| FR-004 | Account identifiers are HMAC-tokenised before persistence. Raw identifiers never reach the database | Must |
| FR-005 | Accepted transactions are persisted and enqueued in one database transaction | Must |
| FR-006 | `POST /v1/transactions/batch` accepts an array; replayed transactions do **not** raise alerts unless `raise_alerts=true` | Must |
| FR-007 | The API is versioned in the path and published as OpenAPI | Must |

### 7.2 Scoring

| ID | Requirement | Priority |
|---|---|---|
| FR-010 | A worker claims queued transactions using `SELECT ... FOR UPDATE SKIP LOCKED` | Must |
| FR-011 | Features are computed by the shared `riskradar.features` package | Must |
| FR-012 | The model returns a **calibrated** probability of fraud | Must |
| FR-013 | The rules layer returns named signals: `{code, severity, evidence{}}` — never points | Must |
| FR-014 | The policy layer maps (probability, signals, context) to a decision and risk level | Must |
| FR-015 | Rules may **escalate**, **override** to CRITICAL, or **suppress** via allowlist | Must |
| FR-016 | A decision record is written before any alert is raised | Must |
| FR-017 | If the model is unavailable the system enters rule-only mode **and raises an alarm** | Must |

### 7.3 Alerting and cases

| ID | Requirement | Priority |
|---|---|---|
| FR-020 | An alert is raised when a decision crosses the actionable threshold | Must |
| FR-021 | Alerts correlate into cases by subject within the correlation window | Must |
| FR-022 | Analysts can open, review, annotate, escalate and set outcomes per the state machine and their role | Must |
| FR-023 | Every state transition is audited with actor, timestamp, from-state and to-state | Must |
| FR-025 | Opening a case is recorded in the audit log with the viewer and time; views are kept out of the case's own history panel [v1.3, base FR-408] | Must |
| FR-024 | Case detail shows all correlated alerts, triggered signals with evidence, model attributions, the customer's behavioural baseline, and **the subject's full transaction timeline across the correlation window — alerted and un-alerted alike** [v1.2] | Must |

### 7.4 Dashboard

| ID | Requirement | Priority |
|---|---|---|
| FR-030 | Alerts stream live to the browser over SSE | Must |
| FR-031 | On reconnect the client sends `Last-Event-ID` and the server replays the gap | Must |
| FR-032 | Transaction volume is presented as aggregates and charts on a fixed interval, plus a searchable table — **not** a live feed of every transaction | Must |
| FR-033 | Queue is sortable by score and filterable by risk level, channel and state | Must |
| FR-034 | Alert volume, case outcomes and model metrics are shown **within the FR-032 aggregate charts — no separate metrics screen** [v1.2] | Should |
| FR-035 | The operations view shows median and mean time from opening to closing a case, over the last 7 days [v1.3, base §10] | Should |

### 7.5 Administration

| ID | Requirement | Priority |
|---|---|---|
| FR-040 | ADMIN can enable/disable rules and change thresholds without a code change | Must |
| FR-041 | Every threshold or rule change is audited with actor, old value and new value | Must |
| FR-042 | Threshold sets are versioned; decisions record the version that applied | Must |
| FR-043 | Every rule carries an owner, rationale, approval date and next-review date, kept across versions; rules with no owner, an overdue review, or no firing in 30 days are flagged. A change may carry its reason, which restarts a 90-day review [v1.3, base FR-504, FR-507] | Must |
| FR-044 | False-alarm rate is reported **per rule and for the model alone**, from analyst outcomes, with 95% ranges and a thin-evidence flag [v1.3, base FR-505] | Must |

---

## 8. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-001 | Sustained ingestion at **50 TPS** with **p95 end-to-end decision latency under 2 seconds** [v1.2] |
| NFR-002 | A burst at **250 TPS for 60 seconds** (5x sustained, 15,000 transactions) is absorbed by the queue with **zero transaction loss** [v1.2] |
| NFR-003 | Load test executed in **Week 4**, not Week 5 |
| NFR-004 | Scoring failure must not reject ingestion; the queue absorbs backlog |
| NFR-005 | No secret, pepper or credential in source control |
| NFR-006 | All money handled as integer minor units end to end |
| NFR-007 | Audit log is append-only, enforced by database grants, and hash-chained |

**Where 50 TPS comes from [v1.2]:** it is 4.32 million transactions per day — roughly **43x the reference operating point** in §17, so the claim is headroom, not a boast. It is chosen to be achievable on a single laptop running Postgres, FastAPI and one scoring worker, because that is the machine this system will actually be demonstrated on.

> **Honesty rule.** These are targets to be *measured* in Week 4, not asserted. If the build machine cannot sustain 50 TPS, the figure is revised **downward and republished before the load test**, with the measured number and the bottleneck named in the QA report. It is never quietly dropped, and the demo never claims a figure that was not tested.

---

## 9. Architecture

```
  Simulator (external client)          Future: bank core / switch
            |                                      |
            +------------------+-------------------+
                               |
                     POST /v1/transactions        <-- auth, idempotency, validation
                               |
                    [ transactions ] + [ queue ]   <-- one Postgres transaction
                               |
                     Scoring worker (SKIP LOCKED)
                               |
        riskradar.features --> model (calibrated) --> rules --> policy
                               |
                        [ decisions ] (immutable)
                               |
                         [ alerts ] --correlate--> [ cases ]
                               |
                        LISTEN / NOTIFY (id only)
                               |
                          SSE --> React dashboard
                               |
                    REST actions --> [ audit_log ] (hash-chained)
```

**Key architectural commitments:**

- **The simulator is a pure external client.** It calls the same public endpoint a bank would. It is a test harness, deletable without touching the product.
- **Postgres is the queue.** `SELECT ... FOR UPDATE SKIP LOCKED`. No Kafka, no Redis, no Celery — one database, one durability story, one backup strategy.
- **SSE, not WebSockets.** Traffic is overwhelmingly server-to-client; analyst actions are request/response REST. Bank proxies break WebSocket upgrades, and WebSocket reconnection has no replay. SSE `Last-Event-ID` gives gap replay at the protocol level.
- **`LISTEN`/`NOTIFY` carries IDs only.** The 8000-byte payload cap and non-durability mean the table is the record and the notification is only a doorbell.

---

### 9.1 Source systems [v1.1]

Risk Radar's event source is the **channel and switch layer**. A core banking system is an **enrichment** source, not an event source.

| Layer | Supplies | Risk Radar's use |
|---|---|---|
| **Channel** — mobile app backend, USSD gateway, internet banking, agent app | device fingerprint, IP region, session context, true channel | **Event source.** Origin of `device_token`, `ip_region`, `channel` |
| **Switch** — NIP gateway to NIBSS, card switch (ISO 8583) | authorisation attempts **including declines**, rail, MCC, card-present flag | **Event source.** The only layer where card testing is observable |
| **Core banking** | customer-to-account hierarchy, account open date, product type, dormancy, branch, posted balance | **Enrichment and ground truth.** Not the event feed |

**Why the core cannot be the event source:** a core banking system is a ledger. It records postings, and it has no device fingerprint, no IP region, no reliable channel indicator, no merchant category, and — decisively — **no declined authorisations**, because a decline never posts. Half of the canonical event in §6.1 does not exist there. More fundamentally, detection at the ledger is detection after the money has moved; the channel and switch layers are the only ones that see an event *before* authorisation, which is also the only place the enforcement seam of decision D7 could ever be connected.

**Typology observability**, recorded so the claim is never overstated: **mule fan-out** is fully observable at the core; **account takeover** only partially, since the compromise, the device change and the balance-check reconnaissance are not financial transactions; **card testing** barely, since declines never post and approved probes arrive as next-day settlement. All three remain in scope — they are synthetic in v1 and require no source system.

Because the switch layer is the only place a **declined** authorisation exists, the canonical event carries `auth_result` and `decline_reason` (§6.1). This is what makes card testing representable at all, and it is the sharpest single justification for consuming the switch rather than the core.

**Ledger-leg invariant.** A core banking system posts double-entry: one customer transfer becomes a sender debit, a settlement credit, a fee debit and a tax debit. Treating each leg as an event would make one transfer look like four transactions inside the velocity window — `VELOCITY_BURST_24H` firing on bank fees. Under this section Risk Radar never sees legs, because the channel layer emits one event per customer action. The invariant is recorded for any future core adapter: **one event is one customer-visible movement of value, never a ledger leg.**

The simulator therefore emits **channel-layer events enriched with core-derived account attributes**, which is the shape a real deployment would consume. Detail and caveats in [core-banking-reference.md](core-banking-reference.md).

---

## 10. Data and privacy

| Decision | Detail |
|---|---|
| Tokenisation | HMAC-SHA256 with a secret pepper, applied at the ingestion boundary. Deterministic, so behavioural baselines and future ring detection still work — they need *stable* identity, not *readable* identity |
| Reversibility | **None.** No mapping table, no key custody. Risk Radar cannot resolve a token to an account |
| Escalation | The **token travels**; the receiving bank performs the lookup on their side |
| Analyst UI | A sparse `display_name`, populated by the simulator with obviously-fake names |
| v1 data | Synthetic only |

**Why one-way:** this keeps the system NDPA-defensible on the day real data arrives, with no migration — and keeps the team out of the business of holding a decryption key for account numbers.

**Before any real data is ingested,** a retention schedule and lawful-basis assessment under NDPA 2023 are required. Both are out of scope for v1 and listed in the roadmap.

---

## 11. Detection design

### 11.1 Three layers, no blended score

**[CORRECTION]** The Implementation Plan says rules and the model "combine to produce a 0–100 risk score." A weighted blend is the wrong construction:

- It is a **type error** — rule severity is ordinal, model output is cardinal; adding them yields a number meaningful in neither system.
- It **destroys calibration** — a calibrated probability lets you predict alert volume at a threshold; a blend cannot, and alert volume is what decides whether analysts drown.
- It **cannot be explained** — decomposing a blended 71 means showing the components anyway.
- It makes **tuning global** — nudging one rule's weight shifts the meaning of every band boundary at once.

Instead:

| Layer | Output |
|---|---|
| Model | Calibrated `P(fraud)` — isotonic or Platt, with a reliability curve reported |
| Rules | A set of named signals: `{code: VELOCITY_BURST_24H, severity: HIGH, evidence: {count: 7, threshold: 4, window: "24h"}}` |
| Policy | Explicit, readable, versioned function: (probability, signals, context) to decision + risk level |

The 0–100 score still exists, derived from `P(fraud)`, as a **presentation artefact for sorting**. It is not a decision input.

### 11.2 Rule powers

- **Escalate** — a signal raises the band (velocity burst pushes MEDIUM to HIGH)
- **Override** — a deterministic veto goes straight to CRITICAL regardless of the model (sanctioned beneficiary, known mule). Some things are not probabilistic
- **Suppress** — an allowlist lowers a band (pre-registered beneficiary, salary credit). **This is the primary false-positive control and must be built, not deferred**

### 11.3 Thresholds

Configuration in the database, versioned, every change audited. **Derived backwards from an alert budget** — analysts multiplied by reviewable alerts per day — then find the threshold yielding that volume at acceptable recall. Never "80 sounds high."

**The budget is 120 alerts per day** [v1.2]: a three-analyst desk at 40 reviewable alerts each. Against the reference operating point in §17 — 100,000 transactions per day — that is an alert rate of **0.12%**, and every threshold in the system is solved backwards from it.

---

## 12. ML methodology

### 12.1 The circularity problem, and the control for it

If the simulator labels fraud by threshold — high amount, new device, velocity — and the model trains on that data, the model learns the thresholds you already have, for free, by hand. You get a 0.99 AUC that means nothing, and the ML half of a system called Risk *Intelligence* is decorative.

**Control:** the simulator models criminal **processes**, not detection thresholds. The label is "emitted by a fraud process." Detection never sees the process, only its shadow.

**Integrity rule — non-negotiable:** the generator and the rules engine are written by different people from different specs, and **no generator parameter is ever read by detection code.**

### 12.2 Fraud typologies

Three, chosen to be structurally distinct — which is what makes held-out-typology evaluation meaningful.

| Typology | Latent process |
|---|---|
| **Account takeover** | Compromise event, new device, reconnaissance (small balance-check transactions), dormancy, then burst extraction across multiple beneficiaries — **and, where the victim holds more than one account, across the victim's own accounts** [v1.1] |
| **Mule network fan-out** | One account fanning funds to many fresh beneficiaries within minutes |
| **Card testing** | Low-value authorisation probes — predominantly **declines** (`auth_result = DECLINED`) — preceding a large authorisation [v1.1] |

### 12.3 Evaluation

- **Hold out typologies, not rows.** Standard splits leak: if account takeover appears in both halves you are measuring memorisation. Train on some typologies, test on one never seen.
- **Publish the held-out number**, not the flattering in-distribution one. It is the only figure that honestly answers "will this catch fraud we did not anticipate?"
- **Metrics: PR-AUC, recall at a fixed alert budget, alerts per analyst per day.** **Accuracy is banned** — at a 0.5% base rate, predicting "never fraud" scores 99.5%.
- **The headline recall figure is measured per _incident_, not per transaction** [v1.2]. Correlation (§6.2) means one alert is enough to open the case, and FR-024 then puts the subject's whole timeline in front of the analyst — so catching 1 of an incident's 11 transactions catches the incident. Transaction-level recall at the alert budget lands near 20% and is a **misleading denominator**; incident-level recall over the same alerts is the number that answers "did we catch the fraud?" Both are published, with this explanation attached, so the low number is never discovered by an examiner instead of volunteered.
- **Public validation: IEEE-CIS.** Real chargeback labels; validates methodology (imbalance handling, calibration, threshold discipline), not feature semantics, since its features are anonymised. PaySim documented as planned follow-on for behavioural transfer testing.

### 12.4 Rejected data source

`electricsheepafrica/Nigerian-Financial-Transactions-and-Fraud-Detection-Dataset` (Hugging Face, 5M rows) is **rejected for training and validation**:

- **Synthetic** — stated on the dataset card. Validating a simulator against another simulator does not break circularity.
- **Target leakage** — `persona_fraud_risk`, `location_fraud_risk`, `merchant_fraud_rate`, `channel_risk_score` are almost certainly computed from `is_fraud`.
- **Pre-computes the contribution** — `spending_deviation_score`, `velocity_score`, `geo_anomaly_score` are the behavioural feature layer, supplied by a generator whose formula cannot be read. That breaks the explainability promise.
- **Licence unresolvable** — YAML says `other`, body says GPL, and the card also says original source rights remain with the original publisher.
- Fraud base rate unverified; the statistics endpoint returned HTTP 500.

**Retained as a Nigerian realism reference for the simulator only** — location distributions, merchant categories, channel mix, plausible NGN amount ranges.

> **Standing rule:** never train on a third-party column whose name contains "risk", "score", "rate" or "fraud" unless the code that produced it can be read.

### 12.5 Train/serve skew

The single most likely way this model fails. IRE computes `velocity_24h` in pandas; the worker computes it in SQL. Two implementations, two people, two mental models — they will diverge, and the model will receive features in production that are subtly not what it was trained on. Offline metrics will keep looking fine, because offline evaluation keeps using the offline implementation.

**Control:** one package, `riskradar.features`, of pure functions taking a transaction plus its history and returning a feature vector. Training feeds it a dataframe; the worker feeds it a Postgres query. **Same functions, different data access.**

**Enforcement:** a test computing features through both paths on a shared fixture and asserting equality. This is the highest-value test in the suite and cannot be retrofitted once both paths exist.

**The failure this test does not catch [v1.1]:** equality between the two paths proves they agree; it does not prove they are both right. If both join a mutable account dimension, both read attributes as they stand *now*, both are point-in-time-leaked, and the equality test passes happily. This is why §6.1 stamps account context at the ingestion boundary and forbids `riskradar.features` from joining any mutable table — the property is enforced by **schema shape**, not by a test, because no equality test can see it.

---

## 13. Security and audit

| Area | Decision |
|---|---|
| Password hashing | Argon2id |
| Sessions | **Server-side, stored in Postgres**, delivered via `httpOnly` + `Secure` + `SameSite=Lax` cookie. Revocation is a `DELETE` |
| Not used | JWT in `localStorage` — readable by any XSS and **cannot be revoked** before expiry. Unacceptable for a console showing financial data |
| MFA | TOTP via `pyotp`. Mandatory for `FRAUD_OPS_LEAD` and `ADMIN`, default-on for `ANALYST` |
| SSO | `external_idp_subject` column added now as an **OIDC seam**. The socket, not the integration |
| Audit storage | Application database role holds `INSERT` only on `audit_log` — **no UPDATE or DELETE grant at the database level**. A separate migration role owns the schema |
| Tamper evidence | Rows hash-chained: each stores `prev_hash` and `SHA256(prev_hash || payload)`. Tampering breaks the chain and is found by query |
| Actor | **Never null.** System-generated actions use a reserved system principal |

**Why this is not optional:** an audit trail without authenticated identity is not an audit trail. "Analyst cleared this case" means nothing if anyone can assert they are any analyst — and non-repudiation is the entire value of the audit feature.

---

## 14. Delivery plan

**[CORRECTION]** The Implementation Plan's Week 1 builds four foundations in parallel that meet later, while its own risk register rates "backend, model and frontend integrated too late" as Medium/High with the mitigation "deliver a thin end-to-end flow in Week 1." The plan does not do this. It now does.

| Week | Deliverable | Owner |
|---|---|---|
| **1** | **Thin vertical slice, end to end.** One authenticated transaction ingested, queued, scored by a **stub model returning a constant**, alert raised, correlated into a case, visible in a logged-in dashboard, with an audit row. Ugly, unstyled, no ML. Schema v1, auth, RBAC skeleton | All |
| **2** | Latent-process simulator with three typologies; `riskradar.features` package with the equality test; rules layer with **6 rules** including suppression; Figma flow approved | IRE, Chidera, Paul |
| **3** | Trained calibrated model replacing the stub; policy layer; audited thresholds; SSE live alert stream; queue and case list UI | IRE, Chidera, Paul |
| **4** | Case detail with explanations; case workflow and outcomes; hash-chained audit complete; aggregate charts; **load test against NFR-001/002**; IEEE-CIS validation | All |
| **5** | Feature freeze Monday. QA, held-out-typology evaluation published, demo-data curation, documentation, rehearsal | All |

**Scope freezes at the end of Week 1.** Anything not in section 4.1 moves to the roadmap.

### 14.1 Definition of done

A component is done when it has: tests, an audit trail where it changes state, error handling that fails loudly, no secrets in source, and a migration if it touches the schema.

---

## 15. Risks

Carried forward from the Implementation Plan, re-rated after this review.

| Risk | Was | Now | Why it changed |
|---|---|---|---|
| Behavioural feature queries slow scoring | High | **Medium** | Advisory, not blocking — no hard latency ceiling |
| Real-time dashboard updates unstable | Medium | **Low** | SSE auto-reconnects with `Last-Event-ID` replay |
| Frontend/backend schema mismatch | High | **Low** | Week 1 vertical slice forces the contract immediately |
| Backend/model/frontend integrated too late | High | **Low** | Same |
| ML model poor recall / false positives | High | **High** | Unchanged. Mitigated by rule-only fallback and alert-budget threshold derivation |
| Synthetic data not realistic enough | High | **High** | Unchanged. Mitigated by latent-process design and the realism reference corpus |
| Scope creep | High | **High** | Unchanged, and now the dominant risk. Mitigated by the Week 1 freeze |
| **New: model learns the simulator's rules** | — | **High** | Generator/detector wall, held-out-typology evaluation, IEEE-CIS validation |
| **New: train/serve skew** | — | **High** | Shared feature package plus equality test |
| **New: identity workstream unplanned** | — | **Medium** | Owned by Chidera; compensating cuts made in section 4.2 |
| **New [v1.1]: core knowledge becomes a scope-creep vector** | — | **Medium** | Contained by D18 — the core banking reference may change the simulator and the documents only, never the architecture or the plan |
| **New [v1.1]: enrichment attributes become label proxies** | — | **High** | If the simulator marks only fraud victims dormant, dormancy *is* the label. Controlled by D20c: attributes drawn from population distributions before any fraud process is assigned; processes may select on them, never set them |
| **New [v1.1]: decline volume overwhelms the alert budget** | — | **Medium** | Probing bursts are decline-heavy. Controlled by D21b — declines feed features by default; only the policy layer promotes them to alerts |
| **New [v1.1]: point-in-time leakage via account joins** | — | **High** | Invisible to the §12.5 equality test — both paths would be wrong identically. Controlled by D22 structurally: context stamped at the boundary, stored as timestamps not derived values, feature package forbidden from joining mutable tables |
| **New [v1.1]: identity workstream now oversubscribed** | — | **Closed** | **[v1.2]** Compensating cut landed — see §4.2 and D25. D12d closed |
| Hosting or internet fails during the defence | Medium/High | **Closed** | **[v1.2]** Eliminated rather than mitigated: the demo is local-only by decision (§17.4). There is no hosting to fail |

---

## 16. Roadmap beyond v1

| Item | Trigger |
|---|---|
| Enforcement / blocking | A bank sponsor and a signed fail-open policy |
| Fraud-ring graph analysis | v1 stable; Elliptic dataset for validation |
| Shadow mode and PSI drift | A second model exists |
| PaySim behavioural validation | Feature pipeline stable |
| OIDC / bank SSO | Bank engagement |
| NDPA retention schedule and lawful-basis assessment | **Before any real data is ingested** |
| CBN / NIBSS / NFIU reporting | Bank engagement |
| Nigerian scam-refund clocks and BVN watchlist (base REG-NG-04/05/06), as versioned configuration | Clock values confirmed against the CBN circular (D69g) |
| Non-payment events: login, device binding, new payee, SIM change (base FR-102) | Event contract extended with the channel team (D69h) |
| Screening incoming payments for mule proceeds (base FR-209, REG-NG-08) | Receiving-leg feed from the switch (D69h) |
| Unsupervised anomaly layer as a novelty safety net (base FR-203) | Held-out evaluation arm shows it adds recall inside the budget (D69h) |
| Maker–checker approval on rule and threshold changes (base FR-308) | Before any bank pilot |

---

## 17. Settled parameters [v1.2]

Nothing in this PRD is open. The three questions that stood here are answered below, and every branch in [decisions.md](decisions.md) is closed.

### 17.1 Reference operating point

The synthetic world the system is tuned against. Every threshold, every budget and every metric in this document is solved against these numbers.

| Parameter | Value |
|---|---|
| Customers | 50,000 |
| Accounts | ~110,000 (average 2.2 per customer — the D19 hierarchy must be exercised, not decorative) |
| Transactions per day | **100,000** (≈1.16 TPS average, bursty by time of day) |
| Pre-seeded history | **14 days** (~1.4M transactions) — enough for 24-hour and 7-day velocity features to be meaningful on day one |
| Fraud transaction rate | **0.3%** — ~300 fraud transactions per day |
| Fraud incidents | **~30 per day**, each emitting 8–15 transactions across the three typologies |

### 17.2 Capacity and load

| Parameter | Value |
|---|---|
| Sustained ingestion (NFR-001) | **50 TPS**, p95 end-to-end decision latency **< 2s** |
| Burst (NFR-002) | **250 TPS for 60s** — 15,000 transactions, zero loss |
| Headroom over the operating point | **~43x** |
| **Measured 2026-09-09** | **NFR-001 PASS** — 50.2 TPS achieved, end-to-end p50 310 ms, **p95 878 ms**, max 1,692 ms, zero loss. **NFR-002 PASS** — 250.9 TPS for 60 s, 15,000 transactions, queue peaked at 11,992 rows, **zero loss**, backlog cleared in 171 s. Full detail in [qa-report.md](qa-report.md) |
| Note on burst latency | End-to-end latency during the burst reaches minutes, and NFR-002 makes no latency claim on purpose: absorbing a 5x burst in a queue *means* latency grows. The system is advisory (D7), so the designed failure mode under overload is a slower decision, never a dropped transaction (NFR-004) |

### 17.3 Alert budget

| Parameter | Value |
|---|---|
| Analyst desk | **3 analysts** |
| Reviewable alerts per analyst per day | **40** |
| **Alert budget** | **120 alerts/day** |
| Implied alert rate | **0.12%** of transactions |
| Expected mix at budget | ~60 alerts on true incidents, ~60 false positives — ~50% incident-level precision |
| **Measured on the demo, 2026-09-13** | Far below that expectation: of 36 decided cases, 2 were confirmed fraud. The demo has about 3 fraud incidents a day and its familiar-payee rule cannot fire on 3 days of history (D69i), so the budget is spent mostly on false alarms. Per-rule figures are thin (FR-044) and a smaller budget is the team's open choice (D67c) |
| Headline recall metric | **Incident-level**, per §12.3 |

### 17.4 Hosting

**Local only.** Docker Compose on a single machine, with a seeded database snapshot committed as a demo fixture. Not a fallback — the primary and only path.

*Why:* the Implementation Plan rates hosting or internet failure during the defence as Medium/High, and its contingency plan already requires a fully local path to exist. Once that path exists and is rehearsed, a cloud deployment adds a second thing to fail on the day and proves nothing the local run does not. Committing to local **deletes** the risk instead of mitigating it, and buys back the Week 5 time that deployment would have consumed.

---

## 18. Alignment with the enterprise base PRD [v1.3]

A reusable *Base PRD — Enterprise Fraud Intelligence Platform* sets out what a bank, fintech or switch should demand of a full fraud platform. Every one of its 102 numbered requirements is mapped, with evidence, in [base-prd-alignment.md](base-prd-alignment.md). The result: **12 met, 40 partly, 35 not built, 6 rejected for v1, 6 out of scope, 3 not applicable.**

- **Adopted as a traceability frame, not as scope** (D69). Its requirement IDs let this work be traced into a bank's evaluation; D1, D3, D4 and D16 are unchanged.
- **Rejected for v1:** blocking or holding payments before authorisation (base FR-301, FR-302, FR-310, FR-311) because Risk Radar is advisory (D7, D69a); and one weighted score across rules and model (base FR-210) because it destroys calibration (D11, D69b).
- **Not adopted for v1:** the p99 ≤ 300 ms latency target, which is for inline blocking. Our scoring step fits it (p95 23 ms); the asynchronous queue does not, by design (D69c).
- **Built in response:** FR-025, FR-035, FR-043 and FR-044 above (D69d, D69e, D69f, D69k).
- **Largest remaining gaps**, now on the roadmap in §16: Nigerian regulatory clocks and the BVN watchlist (D69g); non-payment events, incoming-payment screening and an unsupervised safety net (D69h).
- **Positioning** (D69j): the base PRD recommends a hybrid, buying the decisioning and case platform and building local fraud models on top. Risk Radar's strongest work is that second half.

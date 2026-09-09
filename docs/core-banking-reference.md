# Risk Radar — Core Banking Reference Model

**Status:** Design-time reference only. Version 1.2. 2026-09-09. **No open questions.**
**Owner:** Chidera (Backend & Database) with IRE (Simulator).
**Feeds:** [PRD](PRD.md) §4.1, §6.1, §9.1, §12.2, §12.5 · [decisions.md](decisions.md) D18–D22, D29.

---

## 0. What this document is, and what it is not

> **We have no access to Finacle, to any Finacle installation, to Finacle documentation, or to any bank's data.**
>
> This document is a **reference model**: a description of what a Nigerian retail core-banking feed plausibly looks like, assembled from general public knowledge of Infosys Finacle's data model and of Nigerian retail banking practice. Table and column names are indicative of the Finacle 10.x era and vary by version and by each bank's customisation. **Nothing here has been verified against any bank's actual system.**
>
> Its only purposes are (1) to shape the simulator so synthetic data has a defensible structural resemblance to a real core, and (2) to demonstrate that Risk Radar's canonical event schema could be populated from real systems. It is **not** evidence of bank engagement, and it does not alter decision **D3 (no signed sponsor)** or the v1 scope in PRD §4.

This document is the *entire* permitted footprint of the "the target bank runs Finacle" input. It changes the simulator and the documentation. It does not change the architecture, the delivery plan, or the scope freeze.

---

## 1. The identity hierarchy

The single most useful thing a core banking data model contributes to Risk Radar. Nigerian retail banking stacks identity in four layers:

```
BVN         one human being, shared across ALL Nigerian banks (NIBSS-level)
 |
 +-- CIF     the customer as known to THIS bank        (Finacle: customer master)
      |
      +-- ACID    one account                          (Finacle: account master)
      |    |
      |    +-- NUBAN   the 10-digit number the customer quotes
      |
      +-- SOL_ID  the service outlet (branch) that owns the account
```

**One customer owns many accounts.** A retail customer routinely holds a salary current account, a savings account and sometimes a domiciliary account, all under one CIF, all under one BVN.

This is the fact that broke the original definition of a case subject. See **D19** and PRD §6.2.

### 1.1 Consequences for tokenisation

Risk Radar tokenises at **two** levels, both one-way HMAC-SHA256 with the pepper, both deterministic (D9c and D9d are unchanged — the token still travels, neither level is reversible):

| Token | HMAC of | Purpose |
|---|---|---|
| `subject_token` | customer identity (CIF / BVN-equivalent) | **Case correlation.** Cross-account ATO. Ring detection in v2 |
| `account_token` | account identity (ACID / NUBAN) | **Behavioural baselines.** Velocity, amount and frequency features |
| `beneficiary_token` | beneficiary account | Fan-out counting, mule detection |
| `beneficiary_subject_token` | beneficiary customer, where known | Intrabank only. Null for outbound NIP |

Correlating on the customer while baselining on the account is the point. A salary current account and a dormant domiciliary account have completely different normal behaviour; merging them into one baseline produces a profile that flags nothing and excuses everything.

---

## 2. What a core banking system has — and does not have

A core banking system is a **ledger**. It records postings. It is downstream of every decision that matters to fraud detection.

| Risk Radar field | Available from the core? | Actually lives in |
|---|---|---|
| `amount_minor`, `currency` | **Yes** | Core |
| `occurred_at` (value / posting date) | **Yes** | Core |
| `account_token`, `subject_token` | **Yes** | Core |
| `beneficiary_token` | Partly — narration-dependent for outbound | Core + switch |
| `rail` (NIP / NEFT / RTGS / intrabank) | Partly — often inferable only from narration text | Switch |
| `channel` (mobile / USSD / web / POS / ATM) | **Unreliably.** Often only in free-text narration or a bank-specific sub-type code | Channel layer |
| `instrument` | Partly | Switch |
| `device_token` | **No** | Channel layer |
| `ip_region` | **No** | Channel layer |
| `merchant_category` | **No** | Switch |
| Declined authorisations | **No — declines never post to a ledger** | Switch |

The bottom four rows are the finding. Half of Risk Radar's canonical event does not exist in a core banking system and never did.

---

## 3. Layer model: where Risk Radar actually sits

| Layer | Supplies | Risk Radar's use |
|---|---|---|
| **Channel** — mobile app backend, USSD gateway, internet banking, agent app | device fingerprint, IP region, session and login context, true channel | **Event source.** Origin of `device_token`, `ip_region`, `channel` |
| **Switch** — NIP gateway to NIBSS, card switch (ISO 8583) | authorisation attempts **including declines**, rail, MCC, card-present flag | **Event source.** The only place card testing is observable |
| **Core (Finacle)** | CIF↔account hierarchy, account open date, product type, dormancy status, SOL_ID, posted balance | **Enrichment and ground truth.** Not the event feed |

**Why this ordering is correct, not a compromise:** detection at the ledger is detection after the money has moved. The channel and switch layers are the only ones that see an event *before* it is authorised — which is also the only place D7's designed enforcement seam could ever be connected. Risk Radar was implicitly designed for this layer all along; the core banking reference model is what made it explicit.

---

## 4. Typology observability

Recorded so no one ever claims in a defence that the core could feed all three.

| Typology (D17) | Observable at the core? | Why |
|---|---|---|
| **Mule fan-out** | **Fully** | Intrabank and NIP debits are real postings. One account to many fresh beneficiaries in minutes is exactly what a ledger shows |
| **Account takeover** | **Partially** | The extraction burst posts. The compromise event, the device change and the balance-check reconnaissance are not financial transactions and never reach the ledger |
| **Card testing** | **Barely** | Declined probes never post. Approved probes arrive via scheme settlement, typically next-day batch — which also defeats the real-time claim. This is why the canonical event carries `auth_result`, and why the **switch**, not the core, is the source (D21) |

All three remain in scope. In v1 they are synthetic and require no source system. This table exists so that the PRD never *implies* one that could not supply them.

---

## 5. Enrichment features contributed by the core

Four features grounded in a real core-banking data model rather than invented. Cheap for the simulator to emit, and each maps to a specific typology.

| Stamped field | Core origin | Derived feature | Signal |
|---|---|---|---|
| `account_opened_at` | account open date | `account_age_days` | **Mule fan-out.** Beneficiary accounts opened days ago |
| `last_activity_at` | last-activity date | days since activity, dormancy band | **Account takeover.** Dormant, then suddenly active |
| `product_type` | account product code | — | Baseline segmentation — a domiciliary account behaves nothing like a salary account |
| `origin_sol_id` | service outlet owning the account | — | Geographic inconsistency **without touching a raw IP** — NDPA-friendly coarse geography |

**Stamped, not joined (D22).** These four are written onto the event at the ingestion boundary and never mutated. A mutable `accounts` dimension exists for the analyst UI and subject lookup, but the feature package never reads it — joining it would make a six-week-old decision reproduce against *today's* dormancy (defeating G4) and would leak post-fraud account state backwards into training. Note that the first two are stored as **timestamps, not as an age or a status**: a timestamp is a fact that never changes, so deriving the feature from `occurred_at` is point-in-time correct by construction.

*Nigerian practice bands accounts as inactive and then dormant at roughly six and twelve months without customer-induced activity. Confirm current CBN figures before quoting them — the design depends only on deriving the band from `last_activity_at`, not on the exact boundary.*

### 5.1 Integrity control — enrichment must not become a label proxy

**D10a** (the generator/detector wall) forbids detection code from reading any generator parameter. Enrichment fields introduce a subtler version of the same leak: if the simulator sets `dormancy_status = DORMANT` **only** on accounts it has selected as ATO victims, then dormancy *is* the label, the model learns a one-column shortcut, and the held-out-typology evaluation in D10b is meaningless.

**Rule:** every enrichment field is produced by the same population-level generator logic for fraudulent and legitimate accounts alike. Dormancy, account age and product type are drawn from the customer population's own distributions **before** any fraud process is assigned. Fraud processes may *select* accounts using these attributes — a real attacker does prefer a dormant account — but may never *set* them.

This is the same standing rule as **D10f**, applied to our own generator instead of a third party's.

---

## 6. Questions raised by this model — all closed

| # | Question | Status |
|---|---|---|
| CB-1 | A ledger is **double-entry**: one transfer is one transaction id carrying multiple part-transaction legs — sender debit, settlement credit, fee debit, tax debit — each with its own serial number. What is the canonical mapping from legs to events? | **RESOLVED — D21c.** Dissolved by D20: the channel layer emits one event per customer action, so Risk Radar never sees legs. Retained as an invariant for any future core adapter — **one event is one customer-visible movement of value, never a ledger leg.** Treating legs as events would fire `VELOCITY_BURST_24H` on bank fees |
| CB-2 | Real integration would be message- or CDC-based (MQ, change data capture off the ledger), not a REST push. Where does the adapter live? | **CLOSED — D29a.** v2, no v1 work. Any future adapter sits on the bank's side of the wire, honours the D21c leg invariant, and performs the D22 boundary stamping there |
| CB-3 | `beneficiary_token` for outbound NIP is often only recoverable from narration text. Does the simulator model that, or assume a clean field? | **CLOSED — D29.** Clean field. Narration recovery is a property of a core adapter, and D20 places Risk Radar at the channel/switch layer where the beneficiary arrives structured. Closed by scope, not overlooked |

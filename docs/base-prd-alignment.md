# Risk Radar against the Enterprise Base PRD

**Date:** 2026-09-13 · **Decisions:** D69–D69k in [decisions.md](decisions.md) · **Our PRD:** [PRD.md](PRD.md) §18

The *Base PRD — Enterprise Fraud Intelligence Platform* is a reusable checklist for
what a bank, fintech or switch should demand of a complete fraud platform. This
document holds Risk Radar up against every numbered requirement in it and says,
plainly, where we stand.

## What it means for the project, in five points

1. **It is a frame, not our scope.** The base PRD describes a platform a bank buys or
   builds over twelve months. Risk Radar is a five-week, advisory, first version on
   invented data (D1, D3, D4, D16). We use its requirement numbers so our work can be
   traced into a bank's own evaluation, and we do not widen scope to chase all of it.
2. **We disagree with it in two places, on purpose.** It asks the system to *block or
   hold* payments before they complete (FR-301, FR-310, FR-311), and to blend rules
   and models into *one weighted score* (FR-210). Risk Radar advises and never blocks
   (D7), and keeps the rules, the model and the decision separate (D11). Both stay, and
   the reasons are below (D69a, D69b).
3. **We built what it ranks highest and we could do honestly now.** It says the first
   question that ends a vendor conversation is *"show me false positives by rule, not in
   aggregate."* That now exists (D69d), with an owner, a reason and a review date on
   every rule (D69e), median time to resolve a case (D69f), and a permanent record of
   who opened each case (D69k).
4. **Its Nigerian requirements expose our largest gaps.** The regulator's clocks for
   authorised-push-payment scams, screening money *coming in* as well as going out, and
   events that are not payments (logins, new devices, SIM changes) are where a Nigerian
   bank would find Risk Radar short (D69g, D69h). The clocks are now built (D71).
5. **It tells us which half of the market we are.** Its recommended default is
   *hybrid*: buy the blocking, orchestration and case-workflow platform; build the
   local fraud models on top, because vendors are *"weak at your fraud."* Risk Radar's
   strongest work (the Nigerian event model, three local fraud patterns, testing on
   fraud hidden from training) is exactly the half it says to build (D69j).

## Coverage at a glance

Counted over the 102 numbered items in sections 2, 5, 6, 7.1 and 8 of the base PRD
(acceptance criteria are shown in the tables but not counted).

| Status | Count | Meaning |
|---|---|---|
| Met | 18 | Built and tested, 4 of them added for this review and the rest since (D71, D77, D83) |
| Partly | 52 | Some of it built; the missing part is named |
| Not built | 20 | Absent; on the roadmap or waiting on a bank |
| Rejected for v1 | 6 | Conflicts with a locked decision; kept out on purpose |
| Out of scope | 6 | Belongs to the bank's customer channels, not a fraud engine |
| Not applicable | 3 | Assumes a live bank deployment |

In short: Risk Radar fully meets about one requirement in seven and partly meets
four in ten. That is the expected shape for a five-week first version of a platform
the base PRD itself says takes twelve months, and it is why the five points above name
the few gaps that matter most rather than all of them.

---

## 2. Objectives

| ID | Objective | Status | Where Risk Radar stands |
|---|---|---|---|
| O1 | Stop fraud before settlement | Rejected for v1 | Advisory only (D7). We return ALLOW / MONITOR / REVIEW / HOLD beside every score as a seam for a bank to enforce. |
| O2 | Fewer false alarms without less detection | Partly | Measured together: held-out recall 0.892 at 120 alerts a day, with budget trade-offs at 75 and 60 (D67c). Now reported in the unit banks benchmark: 15:1 false alerts per incident at 120, 6.6:1 at 75, 4.1:1 at 60, on the Analytics screen (D70). Not yet reduced on live outcomes. |
| O3 | One decision layer for every channel | Partly | One scorer for every channel and instrument in the event model (mobile, web, USSD, POS, ATM, agent, branch). Non-payment events are accepted through one envelope (D72) and read by the features and rules that score payments (D77). |
| O4 | Regulatory clocks met automatically | Partly | CBN scam clocks run on every reported case, from versioned configuration and a Nigerian working-day calendar; breaches are recorded and escalated (D71). Meeting them still takes the desk: nothing is notified or paid automatically. |
| O5 | Lower cost per investigation | Partly | Ranked worklist, one-key outcome, case assignment; median time to resolve measured (D69f); machine actions take 24 alerts a day off the analysts at the same detection (D80). Cost per case not measured. |
| O6 | Every decision defensible | Met | Every decision stores its rules' signals with evidence, the policy trace, feature attributions and the versions that produced it (D7a). |

## 5.1 Data ingestion (FR-1xx)

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| FR-101 | Real-time events from every payment rail | Partly | One API accepts card, transfer, cash and wallet across NIP, NEFT, RTGS, card scheme, in-bank and ATM rails. Cheques absent; no live rail connected. |
| FR-102 | Non-payment events: logins, resets, device binding, new payee, limit and SIM changes | Met | Accepted through `POST /v1/events` (D72) and read by five features and a takeover sequence rule (D77); unseen account takeover 0.726 to 0.986 at 75 alerts a day. Limit changes are stored, not yet read. |
| FR-103 | Device intelligence | Partly | Hashed device fingerprint and coarse region. No emulator, rooting or tampering signals. |
| FR-104 | Behavioural biometrics | Not built | — |
| FR-105 | Identity bureaus and registries | Not built | Needs a bank (D3). The adapter seam exists (D75, INT-05); no bureau is connected. |
| FR-106 | Rolling profiles per customer, account, device, payee, merchant, agent, IP | Partly | Account 30 days, customer 30 days, destination 90 days (feature spec 1.1.0). No merchant, agent or IP profiles. |
| FR-107 | Streaming and batch, with replay for backtesting | Met | Batch endpoint; replayed history is scored without raising alerts (D8d). |
| FR-108 | Data-quality monitoring | Partly | Invalid payloads go to a dead-letter table (FR-003); both event and arrival times are stored, so lateness is measurable. No automatic alerting. |
| FR-109 | External threat intelligence feeds | Partly | Sanctioned and known-mule lists, managed by hand. No external feed. |

## 5.2 Detection (FR-2xx)

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| FR-201 | Rules engine | Partly | Thirteen rules: bursts, card testing, takeover sequence, credit fan-in, the second leg, and four added for Nigerian fraud types (D82): scam collection account, SIM swap transfer, dormant account, card cash-out in a new region, each tuned by a stated criterion. A new kind of rule still needs code. |
| FR-202 | Supervised model on labelled outcomes | Partly | Calibrated gradient boosting (D60–D61), trained on simulator labels. Analyst outcomes are recorded as future labels (D13c) but not yet trained on. |
| FR-203 | Unsupervised anomaly detection for novel patterns | Partly | An isolation forest that never sees a label is an arm of the dataset evaluator (D81), measured against the model on files we did not make. It does not run live. |
| FR-204 | Graph and link analysis for rings and mules | Partly | The two-leg link built without a graph (D79): a credit fan-in and the payment that moves it on, scored and cased together; mule-ring value flagged 63.5% to 84.9%. Ring-wide graph analysis still roadmap (D16). |
| FR-205 | Scoring against the individual's own baseline | Met | Amount against the account's usual largest, today against a normal day, new device for this customer, new payee for this account. |
| FR-206 | Session modelling to catch takeover in progress | Partly | Sequences inside a 72-hour window: failed logins, a new device bound, a SIM or credential change, then a payee enrolled and paid (D77). No explicit session identifier from the channel. |
| FR-207 | Scam (APP) detection | Partly | Social engineering is a simulated fraud type with group collections as its legitimate twin, and a rule on a new account several customers paid today (D82). The rule is deliberately narrow (13% of scams at precision 0.31), so the model carries most scams; no session or coached-customer signals. |
| FR-208 | Synthetic identity at onboarding | Not built | No onboarding events. |
| FR-209 | Screening money coming in, not only going out | Partly | Credits are ingested and decided as INBOUND transactions with receiving-side features and a fan-in rule: 73% of mule rings flagged at 7.3 credit alerts a day (D78). Rules only; no model on incoming fraud. |
| FR-210 | One weighted score with visible contributions | Rejected for v1 | Conflicts with D11. Its intent, visible contribution per component, is met by separate outputs and the policy trace (D69b). |
| Accept. | A documented detection path and owner per top fraud type | Partly | Seven fraud types, each with a rule and a measured path (D63–D67, D77–D79, D82). Detection is measured on simulated fraud; independent confirmation waits for IEEE-CIS (D81c). |

## 5.3 Decisioning (FR-3xx)

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| FR-301 | Approve / step-up / hold / decline before authorisation | Rejected for v1 | D7. The seam is now a contract: a directive per decision with a TTL, a signed fail-open policy and a delivery record (D74). Nothing is enforced. |
| FR-302 | Risk-based step-up authentication | Rejected for v1 | Requires enforcement. |
| FR-303 | Policy per channel, product, segment and amount | Partly | A versioned disposition policy per signal: machine action, analyst review or auto-close (D80); analyst reviews a third lower at the same detection. One threshold set still serves every channel and segment. |
| FR-304 | Business users change rules without a deployment | Partly | Enable, disable, retune and re-grade rules from the admin screen, versioned. A new kind of rule still needs code. |
| FR-305 | Shadow mode on live traffic | Partly | Built as a contract (D74): every live decision issues a SHADOW directive with a TTL and fail-open action; delivery and the bank's acknowledgement are recorded and measured. No bank traffic yet. |
| FR-306 | Champion–challenger routing | Not built | — |
| FR-307 | Threshold simulation with projected alert volume | Partly | `scripts/derive_thresholds.py` projects alerts a day and false alarms before publishing (D61b, D67c). Not on the admin screen. |
| FR-308 | Versioning, maker–checker approval, rollback | Partly | Every change is a new audited version. No second approver; rollback is republishing an earlier version. |
| FR-309 | Defined degraded mode | Partly | If the model fails the system runs on rules alone and raises an alarm (FR-017). Not configurable per channel. |
| FR-310 | Action API: block card, freeze account, hold funds | Rejected for v1 | D7. |
| FR-311 | Cooling-off delay on risky first-time transfers | Rejected for v1 | D7; enforcement. |

## 5.4 Case management (FR-4xx)

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| FR-401 | Cases created with context assembled | Met | Timeline, account baseline, signals with evidence, attributions and a recommendation (FR-024). |
| FR-402 | Risk-ranked queues and routing | Partly | Ranked by severity, money and lateness; "take next case" assigns it; escalations route to InfoSec's or the lead's own queue, and recorded outcomes to the lead's awaiting-close queue (D83). No routing by skill or segment. |
| FR-403 | Related alerts merged into one case | Partly | By customer within 24 hours (D13a, D19). Not across a ring of customers. |
| FR-404 | In-case actions | Met | Every recommendation is a checklist of steps, each recorded with its result (customer contacted, card blocked, hold, recall, receiving bank notified, destinations checked, identity verified); escalation with a required reason to InfoSec or the Fraud Ops lead and hand-back with findings; outcome; close (D83). Block, hold and refund are done in the bank's systems and recorded here (D7). |
| FR-405 | Link-analysis view in the case | Not built | — |
| FR-406 | Outcome labels feed model training | Partly | Recorded as labels (D13c); not yet used to retrain. |
| FR-407 | Response clocks with breach alerting, mapped to regulatory clocks | Partly | Regulatory clocks built (D71): versioned policy, working days, breach recorded once, written to the permanent record and escalated to Fraud Ops. The per-severity response clocks are still set in code and not escalated. |
| FR-408 | Tamper-evident record of decisions, overrides and data access | Met | Hash-chained record the application cannot edit, with a check endpoint. Case views are now recorded too (D69k). |
| FR-409 | Evidence exchange with other banks | Not built | The hashed identifier is designed to travel (D9d). |
| FR-410 | Customer message templates | Not built | — |
| Accept. | Decide without opening another system | Met | For the three fraud types in scope. |

## 5.5 Model and rule governance (FR-5xx)

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| FR-501 | Model registry | Partly | Version, artefact hash, feature-spec version, metrics, training and promotion dates, who promoted it. Training-data lineage is a simulator seed, not a snapshot. |
| FR-502 | Drift monitoring | Not built | Training medians are stored with each model; no live comparison (D16). |
| FR-503 | Retraining with a human approval gate | Partly | Retraining script and a promotion endpoint a person must call. Not scheduled. |
| FR-504 | Rules inventory: owner, reason, approval, next review | Met | Built (D69e). |
| FR-505 | False-positive rate per rule and per model | Met | Built (D69d), with 95% ranges and a thin-evidence flag. |
| FR-506 | Dated tuning log with data, impact and sign-off | Partly | Every change is recorded with old and new values and now its reason; large changes are logged with evidence in decisions.md. No sign-off step. |
| FR-507 | Flag rules with no owner or no firing | Met | Built (D69e). It found both risk-lowering rules silent in the demo (D69i). |
| FR-508 | Bias testing on declined populations | Not built | No demographic data; nothing is declined. |
| Accept. | Reproduce a decision from 18 months ago | Met | Each decision stores the rule, threshold, model and feature-spec versions that produced it (D7a). |

## 5.6 Reporting (FR-6xx)

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| FR-601 | Executive loss dashboard | Not built | Money at risk is shown; loss, recovery and chargebacks need a bank. |
| FR-602 | Operational dashboard | Met | Alerts against capacity, backlog, waiting times, workload per analyst, past-due cases, time to resolve. |
| FR-603 | Detection performance per rule, model and fraud type | Partly | Per rule and model live (D69d); per fraud type offline in the held-out evaluation. False alerts per confirmed incident live and offline; share of fraud value detected offline, per budget (D70). Money recovered needs a bank. |
| FR-604 | Regulator-format exports | Not built | Needs a bank (D28). |
| FR-605 | Ad-hoc query and export | Partly | Transaction search; no export. |
| FR-606 | Board pack | Not built | — |

## 5.7 Intelligence sharing (FR-7xx)

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| FR-701 | Industry watchlists in near real time | Partly | Lists exist; loaded by hand. |
| FR-702 | Cross-institution signals | Partly | The industry watch-list both ways: our flags queued in an outbox for the hub, other institutions' flags received by BVN and shown on cases (D75). Not scored; hub not connected. |
| FR-703 | Negative list with expiry, review and appeal | Partly | Add and remove, recorded. No expiry, review or appeal. |
| FR-704 | Law-enforcement request API | Not built | — |

## 5.8 Customer controls (FR-8xx)

FR-801 to FR-805 (self-service limits, MFA on control changes, in-app fraud reports,
"this wasn't me", in-flow scam warnings) are **out of scope**: they live in the bank's
mobile and web channels, not in a fraud engine. One of them matters to us:
**FR-803**, a customer's fraud report, is what starts the regulator's refund clock, so
it is the event Risk Radar would need to receive (D69g). Until a channel sends it, the desk records it on the case (D71).

## 6. Non-functional

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| NFR-01 | p99 ≤ 300 ms, p50 ≤ 100 ms end to end | Partly | The scoring work itself is p50 14 ms, p95 23 ms. End to end, including the queue, it is p50 310 ms and p99 1,581 ms at 50 payments a second (qa-report). The target is for inline blocking; ours is asynchronous by design (D69c). |
| NFR-02 | Throughput with headroom for 3× peaks | Partly | A 5× burst (250 a second for 60 seconds) lost nothing. Latency during the burst was not claimed. |
| NFR-03 | 99.99% availability | Not applicable | One local machine (D26). Degraded mode is defined. |
| NFR-04 | 100% of transactions scored | Met | Every accepted payment is queued and gets one decision; tested. |
| NFR-05 | Reason codes on every decision | Met | See O6. |
| NFR-06 | Data residency | Not applicable | Local only, invented data. |
| NFR-07 | Security | Partly | Roles with separation of duties, hashed identifiers, two-step sign-in, Argon2 passwords, cookie sessions. No encryption at rest configured; no penetration test. |
| NFR-08 | Retention | Partly | Policy set (D27); the record is never deleted. |
| NFR-09 | Recovery point and time | Not built | — |
| NFR-10 | First channel live in 90 days | Not applicable | No bank. |
| NFR-11 | New data source in two weeks | Partly | One documented event contract; a new source maps to it. |
| NFR-12 | Observability | Partly | Latency, queue depth, rule firings and active model exposed. Score distribution not exposed. |

## 7.1 Nigeria (CBN and NIBSS)

The clock values below are as the base PRD states them, from secondary sources.
**Confirm each against the CBN circular before building anything on it.**

| ID | Requirement | Status | Where Risk Radar stands |
|---|---|---|---|
| REG-NG-01 | Real-time fraud monitoring on all e-channels | Partly | That is what Risk Radar is, on invented data and advisory. |
| REG-NG-02 | Identity checks at online opening and reactivation | Not built | Dormant-account reactivation is a model input, not an identity check. |
| REG-NG-03 | BVN and NIN validation | Partly | BVN and NIN learned at the boundary or from the core, tokenised, and checked on first sighting and high-risk events; the result is recorded (D75). Only a format check runs until NIBSS is connected. |
| REG-NG-04 | BVN watchlist, including the 24-hour temporary flag | Partly | The temporary flag is built (D73): at most 24 hours as a database constraint, ends on its own, customer contact recorded and a missed contact escalated. Placed on the BVN when known, covering every customer record with it; every step queued for the industry watch-list and other institutions' flags received and shown (D75). The NIBSS connection itself is a stub. |
| REG-NG-05 | Scam clocks: notify the other bank in 30 minutes, investigate in 14 working days, reimburse in 48 hours, refund in 16 working days | Met | Built (D71), plus the 24-hour acknowledgement. Values are the CBN exposure draft of 26 Nov 2025, held as policy version 1; re-check against the final circular. |
| REG-NG-06 | Customer reporting window | Met | 72 hours from the first alerted payment to the customer's report, shown on the case as met or late; never escalated, as it is the customer's obligation (D71). |
| REG-NG-07 | NIBSS fraud reporting | Not built | Needs a bank (D28). |
| REG-NG-08 | Controls on fraud proceeds received | Partly | Proceeds arriving are flagged and can carry a hold recommendation through the directive (D74, D78). Holding and returning funds, and settlement withholding, are the bank's and NIBSS's (D78e). |
| REG-NG-09 | Fraud desk workflow and forum reporting | Partly | The desk workflow is built; forum reporting is not. |
| REG-NG-10 | Customer opt-out of instant transfers | Out of scope | A channel control. |

## 8. Integrations

| ID | Integration | Status | Where Risk Radar stands |
|---|---|---|---|
| INT-01 | Core banking | Partly | Designed: account facts stamped at intake (D20, D22, core-banking-reference.md). Not connected. |
| INT-02 | Card switch | Partly | Designed as the event source, the only place card declines are visible (D20). Not connected. |
| INT-03 | Instant payments, both legs | Partly | Sending leg only. |
| INT-04 | Digital channels with session and device context | Partly | Device and region only. |
| INT-05 | Identity bureaus | Partly | Adapters for the core's customer inquiry and NIBSS BVN validation, with a simulated core; real connections are stubs (D75). |
| INT-06 | Contact centre | Not built | — |
| INT-07 | Case workflow | Met | Native. |
| INT-08 | Data lake | Not built | PostgreSQL and local corpus files. |
| INT-09 | Security monitoring (SIEM) | Not built | Information security works inside Risk Radar. |
| INT-10 | Customer notifications | Not built | Live alerts reach the dashboard only. |
| INT-11 | Sanctions screening | Partly | A sanctioned-destination list that overrides everything (D11a). |

---

## The four questions that end sales conversations

| Question | Our answer today |
|---|---|
| Show me false positives **by rule** | **Yes.** `GET /v1/metrics/detection` and the Analytics screen. The demo's figures are thin (36 decided cases) and say so. |
| Show me a decision explained to an analyst in under 10 seconds | **Yes in design:** the recommendation and "why this was flagged" sit at the top of every case. Not yet timed with real analysts. |
| Show me p99 latency at 3× peak | **No.** p99 is 1,581 ms at 1×. At 5× we proved zero loss, not latency. |
| Show me which channel is live in month three | **Not applicable.** No bank. |

## Phase plan and success metrics

On the base PRD's phase plan, Risk Radar is **Phase 1 on invented data**: transfers
and mobile scored, rules plus a first model, case management live, a false-alarm rate
measured. Phase 0, a baseline from the bank's own history, cannot happen without a bank.

| Base PRD metric | Risk Radar today |
|---|---|
| Fraud detection rate (by value) | Offline only: 0.892 of incidents on fraud hidden from training; 65% of fraud value on PaySim |
| Fraud loss in basis points | Not applicable without real losses |
| False-decline rate | Not applicable: nothing is declined. Closest measure: false-alarm rate per rule and model |
| Alert-to-case precision | Measured from analyst outcomes (D69d) |
| Mean time to resolution | Measured: median 116 minutes on the demo's 36 closed cases (D69f) |
| Regulatory deadline adherence | Not built (D69g) |
| Recovery rate, cost per case | Not measured |

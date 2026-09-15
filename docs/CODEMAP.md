# Risk Radar — Code Map

**What this is:** a bridge between the three documents and the 11,146 lines of
code. For every promise the PRD makes, this says which file keeps it and why the
decision log says it was done that way.

Read it in whichever direction you need:

- *"Where is FR-002 implemented?"* → §2, the requirements table.
- *"What does this file do and why does it exist?"* → §3, the file-by-file tour.
- *"Where do I start reading?"* → §1.

---

## 1. Where to start reading

If you read four files, read these, in this order. Together they are about 700
lines and they contain the whole idea.

| Order | File | Why this one |
|---|---|---|
| 1 | `backend/riskradar/features/compute.py` | The twelve behavioural features, as twelve small functions. No database, no framework — just "given this transaction and its history, what is unusual about it". Start here because everything downstream consumes its output. |
| 2 | `backend/riskradar/rules/engine.py` | Six rules that emit *named facts*, never scores. Reading this makes the "no blended score" decision obvious rather than theoretical. |
| 3 | `backend/riskradar/policy/engine.py` | The only file that decides anything. About 100 lines of real logic: take a probability, take some facts, produce a verdict, and write down every step. |
| 4 | `backend/riskradar/worker/scoring.py` | The conductor. Pulls work off the queue, calls the three above in order, writes the decision, raises the alert, correlates the case, notifies the dashboard. |

Everything else is plumbing around those four: getting data in (`api/`),
proving who did what (`audit/`, `security/`), showing it to a human
(`frontend/`), and making the data exist at all (`simulator/`).

---

## 2. Requirement → code

Every "Must" in PRD §7 and §8, and where it lives.

### Ingestion

| Req | Promise | Implemented in | Decision |
|---|---|---|---|
| FR-001 | `POST /v1/transactions`, authenticated by API key | `api/routers/ingest.py` → `ingest_one`; auth in `api/deps.py` → `require_api_key` | D8 |
| FR-002 | Repeat submission is a no-op returning the original result | `ingest.py` → `_persist`, the `ON CONFLICT DO NOTHING` branch | D8b |
| FR-003 | Bad payloads dead-lettered with the raw body, never coerced | `api/schemas.py` (strict types) + `api/app.py` → `validation_to_dead_letter` | D8c |
| FR-004 | Identifiers tokenised before persistence | `security/tokens.py`; called in `ingest.py` → `_to_row` | D9c |
| FR-005 | Persist and enqueue in **one** database transaction | `ingest.py` → `_persist`; both writes share the request's connection | — |
| FR-006 | Batch endpoint; replays do not alert unless asked | `ingest.py` → `ingest_batch`; defaults in `schemas.py` → `BatchIn` | D8d |
| FR-007 | Versioned in the path, published as OpenAPI | `api/app.py`; every router carries a `/v1` prefix | — |
| REG-NG-03, REG-NG-04, INT-05, FR-702 | BVN learned and tokenised at the boundary; BVN-level flags; industry outbox and inbound flags; adapters for core, registry and hub | `identity/adapters.py`, `identity/boundary.py` → `stamp`, `identity/industry_sync.py` → `dispatch`, `pull`, `receive`; `clocks/watchlist.py`; `api/routers/identity.py`; `migrations/0011_bvn_identity_industry_watchlist.sql` | D75 |
| FR-209, REG-NG-08 | Credits as INBOUND transactions; receiving-side features; fan-in rule; credits judged without the model | `api/schemas.py` → `TransactionIn._direction_is_consistent`; `features/compute.py` (five D78 features), `features/sources.py` → `_CREDITS_SQL`; `rules/engine.py` → `mule_inbound_fanin`, `applies_to`; `policy/engine.py` → `apply(model_applies=)`; `simulator/riskradar_sim/credits.py`; `ml/receiving_side.py`; `migrations/0012_inbound_credits.sql` | D78 |
| FR-102, FR-206 | Features from non-payment events; takeover sequence rule; simulator event layer | `features/compute.py` (five D77 features), `features/sources.py` → `_EVENTS_SQL`, `features/indexed.py`; `rules/engine.py` → `account_takeover_sequence`; `simulator/riskradar_sim/signals.py`; `ml/takeover_sequence.py` | D77 |
| FR-301 seam, FR-305 | One directive per live decision; bank fetch, poll and acknowledge; signed fail-open policy | `policy/directives.py` → `issue`, `effective`; `api/routers/directives.py`; `worker/scoring.py` after the decision insert; `migrations/0010_directives.sql` | D74 |
| FR-102 (foundation) | Any event type through one envelope; payments also written as events | `ingest.py` → `ingest_event`, `ingest_event_batch`, `_persist_event`; types in `schemas.py` → `EventIn`; table in `migrations/0008_event_envelope.sql` | D72 |

### Scoring

| Req | Promise | Implemented in | Decision |
|---|---|---|---|
| FR-010 | Worker claims with `SKIP LOCKED` | `worker/scoring.py` → `CLAIM_SQL`, `Worker.claim` | D8a, D41 |
| FR-011 | Features come from the shared package | `features/` — `compute.py` is the only implementation | D15 |
| FR-012 | Model returns a **calibrated** probability | `ml/train.py` → `build_model` (isotonic); loaded by `model/registry.py` | D11 |
| FR-013 | Rules return named signals, never points | `rules/engine.py` → `Signal` | D11 |
| FR-014 | Policy maps (probability, signals, context) → decision | `policy/engine.py` → `apply` | D11 |
| FR-015 | Rules escalate, override or suppress | `policy/engine.py` → `apply`, the three loops | D11a |
| FR-016 | Decision written **before** any alert | `worker/scoring.py` → `score_transaction`, insert order | D7a |
| FR-017 | Model unavailable → rule-only mode **and an alarm** | `worker/scoring.py` → the `ModelUnavailable` branch and `_raise_alarm` | D15d |

### Alerting and cases

| Req | Promise | Implemented in | Decision |
|---|---|---|---|
| FR-020 | Alert raised when a decision crosses the threshold | `worker/scoring.py`, `if result.actionable` | — |
| FR-021 | Alerts correlate into cases by subject, in a window | `cases/correlation.py` → `attach` | D13a, D19 |
| FR-022 | Analysts move cases through the state machine by role | `api/routers/cases.py`; permissions in `security/rbac.py` | D13b, D12b |
| FR-023 | Every transition audited with actor and from/to state | `audit/chain.py` → `append`; called from every transition | D12c |
| FR-024 | Case detail shows alerts, signals, attributions, baseline **and the full subject timeline** | `api/routers/cases.py` → `case_detail`; rendered by `frontend/src/components/CaseView.jsx` and `Timeline.jsx` | D24b |

### Dashboard

| Req | Promise | Implemented in | Decision |
|---|---|---|---|
| FR-030 | Alerts stream live | `api/routers/stream.py`; client in `frontend/src/lib/useStream.js` | D14 |
| FR-031 | Reconnect replays the gap via `Last-Event-ID` | `stream.py` → `_event_source`, the replay block before `LISTEN` | D14 |
| FR-032 | Transactions as aggregates + a searchable table, **not** a live feed | `api/routers/metrics.py`; `frontend/src/pages/Metrics.jsx`, `Transactions.jsx` | D14b |
| FR-033 | Queue sortable by score, filterable | `api/routers/triage.py` → `worklist`; `frontend/src/pages/Triage.jsx`. **Ordering is by exposure, clock and severity, not by score** — see D43 | D43 |
| FR-034 | Metrics folded into the aggregate charts | `frontend/src/pages/Metrics.jsx` | D25 |

### Administration

| Req | Promise | Implemented in | Decision |
|---|---|---|---|
| FR-040 | Enable/disable rules and retune without a code change | `api/routers/admin.py` → `update_rule`; read by `worker/scoring.py` → `active_ruleset` | — |
| FR-041 | Every change audited with actor, old and new value | `admin.py`, every handler calls `chain.append` with `from_state`/`to_state` | — |
| FR-042 | Threshold sets versioned; decisions record which applied | `admin.py` → `create_thresholds` (new version, never an edit); `decisions.threshold_set_id` | D11c |

### Non-functional

| Req | Promise | Verified by | Result |
|---|---|---|---|
| NFR-001 | 50 TPS, p95 end-to-end < 2 s | `scripts/load_test.py` | 878 ms — PASS |
| NFR-002 | 250 TPS burst, zero loss | `scripts/load_test.py --burst` | 0 lost — PASS |
| NFR-004 | Scoring failure must not reject ingestion | `worker/scoring.py` → `_record_failure` (backoff, bounded retries) | — |
| NFR-005 | No secret in source control | `config.py` reads the environment; `.env` is gitignored | — |
| NFR-006 | Integer minor units end to end | `amount_minor BIGINT`; `features/compute.py` sums integers | — |
| NFR-007 | Audit append-only, enforced by grants, hash-chained | `migrations/0002_grants.sql`; `audit/chain.py`; `tests/test_security.py` | Verified |

---

## 3. File-by-file tour

### `backend/riskradar/` — the product

| File | Lines | In one sentence |
|---|---|---|
| `config.py` | 100 | Reads settings and secrets from the environment; nothing is hard-coded. |
| `db.py` | 79 | One connection pool. Postgres is the only external thing this system talks to. |
| **`features/spec.py`** | 48 | The list of twelve feature names and the lookback windows. Change a window here and both training and serving change together. |
| **`features/types.py`** | 74 | The shapes the feature functions accept. Deliberately plain — this type boundary is what stops training and serving diverging. |
| **`features/compute.py`** | 212 | The twelve features themselves. Pure functions: no database, no clock, no framework. |
| `features/sources.py` | 168 | The **only** place training and serving differ: one loads history from SQL, one from a dataframe. Both return the same shape. |
| `features/indexed.py` | 121 | A faster history loader for training, added because the naive one is O(n²) over 600,000 rows — and an unusable training path is how teams end up with a second, different feature implementation. |
| **`rules/engine.py`** | 262 | Six rules. Each returns a named fact with its evidence, or nothing. |
| **`policy/engine.py`** | 213 | Probability + facts → verdict, with every step recorded. |
| `model/registry.py` | 218 | Loads the active model, refuses one trained on a different feature spec, and explains a prediction by ablation. |
| **`worker/scoring.py`** | 548 | The scoring loop. The largest file, and the one that ties everything together. |
| `cases/correlation.py` | 109 | Decides whether an alert joins an open case or starts a new one. |
| **`cases/triage.py`** | 205 | Exposure, SLA clocks, the priority formula, and the recommended action. This is what turns a list of cases into a worklist somebody can actually work. |
| `audit/chain.py` | 189 | Appends a hash-chained audit row, and can walk the chain to find tampering. |
| `security/tokens.py` | 70 | One-way HMAC tokenisation. The reason no account number exists in the database. |
| `security/passwords.py` | 77 | Argon2id hashing and TOTP verification. |
| `security/sessions.py` | 119 | Server-side sessions. Logging out is a `DELETE`. |
| `security/rbac.py` | 95 | Who can do what. The separation-of-duties table, in code. |
| `api/schemas.py` | 207 | The request contracts. Strict on purpose: `"1000"` is not a valid amount. |
| `api/deps.py` | 103 | Per-request database handle, identity, and the permission guard every protected route uses. |
| `api/app.py` | 141 | Wires the routers together and turns a validation failure into a dead-letter row. |
| `api/routers/ingest.py` | 237 | The front door. |
| `api/routers/auth.py` | 133 | Login, logout, who-am-I. |
| `api/routers/cases.py` | 492 | Case detail and the individual workflow actions. |
| **`api/routers/triage.py`** | 400 | The worklist, "hand me the next case", one-call disposition, and the operations view. The endpoints the redesigned console actually runs on. |
| `api/routers/metrics.py` | 205 | The aggregate queries behind the charts. |
| `api/routers/stream.py` | 170 | The live alert stream. |
| `api/routers/admin.py` | 497 | Rules, thresholds, lists, models, users, audit. |

### `backend/migrations/` — the database, in order

| File | What it does |
|---|---|
| `0000_bootstrap.sql` | Creates the two database roles. Not numbered into the sequence because it creates the database the sequence runs inside. |
| `0001_schema_v1.sql` | Every table, every enum, every index. The most valuable single file to read if you want to understand the domain. |
| `0002_grants.sql` | The privilege separation. **This is the file that makes the audit claim true.** |
| `0003_rule_lists.sql` | The sanctions, known-mule and allowlist tables the rules read. |
| `0004_correlation_window.sql` | Corrects a constraint from `0001` that was stricter than the design intended. |
| `0005_immutability_scope.sql` | Corrects the immutability triggers from `0002`, which fired on zero-row statements and blocked the retention policy. |

Migrations 4 and 5 exist because the first version was wrong. They are kept
rather than folded back into `0001` so the record of *why* survives.

### `simulator/riskradar_sim/` — where the data comes from

| File | Lines | In one sentence |
|---|---|---|
| `population.py` | 149 | Invents customers: their accounts, devices, home region, waking hours, and the payees they normally pay. |
| `engine.py` | 353 | Ordinary daily behaviour, plus the three criminal processes. The heart of the whole ML integrity argument. |
| `generate.py` | 143 | Runs a population through a number of days and emits events in time order. |
| `cli.py` | 248 | Four commands: write a training corpus, seed the database, stream live, run a burst. |

### `ml/` — training and evaluation

| File | Lines | In one sentence |
|---|---|---|
| `dataset.py` | 159 | Reads the corpus, tokenises it exactly as ingestion would, and turns it into a feature matrix using the shared package. |
| `metrics.py` | 146 | PR-AUC, recall at the alert budget, incident-level recall, and the reliability curve. No accuracy — it is banned. |
| `train.py` | 218 | Trains, calibrates, evaluates against a held-out typology, saves the artefact, registers it. |
| `evaluate.py` | 144 | Runs the held-out evaluation for all three typologies. Measures the **model**. |
| `evaluate_system.py` | 212 | Same, but measures **model + rules + policy**. This is the file that produced the project's best result. |

### `scripts/` — the things you run

| File | What it does |
|---|---|
| `migrate.py` | Creates roles, database and schema. Run first. |
| `seed.py` | Users, API key, ruleset, thresholds, stub model. Run second. Prints the logins. |
| `run_api.py` / `run_worker.py` / `run_workers.py` | Start the three processes. |
| `totp.py` | Prints a login code. Development convenience only. |
| `derive_thresholds.py` | Solves the thresholds backwards from the alert budget and publishes a new audited version. |
| `load_test.py` | Measures NFR-001 and NFR-002 honestly. |

### `frontend/src/` — the console

| File | Lines | In one sentence |
|---|---|---|
| `lib/api.js` | 104 | Every call to the backend. Note there is nowhere here that stores a credential. |
| `lib/useStream.js` | 51 | Subscribes to the live alert stream and reconnects itself. |
| `components/ui.jsx` | 135 | Risk badges, signal pills, attribution bars, and the policy trace renderer. |
| `pages/Login.jsx` | 102 | Two-step sign-in with a second factor. |
| **`pages/Triage.jsx`** | 190 | The screen an analyst lives on: desk summary, worklist rail, and the case beside it so deciding never costs a page load. |
| **`components/CaseView.jsx`** | 280 | The investigation panel. Leads with the recommendation, keeps the disposition buttons permanently in reach, and puts the evidence one click below. |
| **`components/Timeline.jsx`** | 140 | The customer's transactions plotted against time. A burst and a fan-out have shapes; a table does not. |
| **`pages/Operations.jsx`** | 220 | The Fraud Ops Lead's view — backlog, ageing, analyst load, outcome mix. The stakeholder screen. |
| `pages/Metrics.jsx` | 237 | Volume, latency and model charts. |
| `pages/Transactions.jsx` | 112 | Search. |
| `pages/Admin.jsx` | 422 | Rules, thresholds, models, lists, audit — with a Verify chain button. |

---

## 4. The five ideas the code keeps repeating

If these five make sense, the codebase makes sense.

1. **The boundary tokenises and stamps; the interior never looks anything up.**
   Raw account numbers exist for the duration of one HTTP request and are never
   written down. Account context is frozen onto the event at that same moment,
   so a decision made in September still reproduces in December.

2. **One implementation of the features, two ways of fetching history.** If you
   are ever tempted to compute a feature anywhere except `features/compute.py`,
   the answer is no, and there is a test that fails.

3. **The decision is written first, always, for everything.** Even for the
   boring transactions nobody will look at. The alert table is a *view* of what
   was interesting, never the record of what happened.

4. **Configuration is versioned, never edited.** Change a rule and you get
   ruleset v2; decisions keep pointing at v1, because that is what actually
   produced them.

5. **The database enforces what matters.** Not the application. The app role
   physically cannot rewrite the audit log, and no amount of application bugs
   changes that.

---

## 5. What each document is for

| Document | Answers |
|---|---|
| [PRD.md](PRD.md) | *What are we building and to what standard?* Requirements, scope, settled parameters. |
| [decisions.md](decisions.md) | *Why is it built this way?* D1–D42, each with the alternative that was rejected. |
| [core-banking-reference.md](core-banking-reference.md) | *How would this attach to a real bank?* And the caveats on that model. |
| [qa-report.md](qa-report.md) | *Does it work, and how well?* Measured results and known limitations. |
| **CODEMAP.md** (this file) | *Where is any of that in the code?* |
| [README.md](../README.md) | *How do I run it?* |

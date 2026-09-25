# Risk Radar

Real-time transaction fraud detection for a Nigerian retail banking context.

Risk Radar ingests transactions, scores each one with a calibrated model combined
with deterministic rules, and surfaces suspicious activity to fraud analysts as
explainable alerts grouped into investigable cases — with a tamper-evident audit
trail behind every decision.

**It is advisory.** It produces a decision (`ALLOW` / `MONITOR` / `REVIEW` /
`HOLD`) with its reasons. It never blocks, holds, reverses or freezes anything;
enforcement belongs to the caller.

---

## Documentation

| Document | What it holds |
|---|---|
| [docs/PRD.md](docs/PRD.md) | Product requirements — the build baseline |
| [docs/decisions.md](docs/decisions.md) | Every decision D1–D41 with its rationale and rejected alternative |
| [docs/core-banking-reference.md](docs/core-banking-reference.md) | Where the system sits relative to a core banking system, and the caveats on that model |
| [docs/qa-report.md](docs/qa-report.md) | Measured results, test coverage, known limitations |

---

## Running it

Requires **Python 3.12+**, **Node 20+**, and **PostgreSQL 16**. There is no
Docker requirement — the demo is local by decision (D26).

### The short way, once it is set up

```bash
.venv/Scripts/python scripts/up.py      # everything, ~1 minute
.venv/Scripts/python scripts/down.py    # stop it again
```

`up.py` starts PostgreSQL on its port, applies any pending migrations, then
brings up the API, the scoring workers, the clock sweep, the live transaction
feed and the frontend — checking each one actually came up — and prints
http://localhost:5173. On Windows, `scripts/up.cmd` does the same on a
double-click. It reuses the database already on disk: **it rebuilds nothing.**

For a clean desk, restore a snapshot rather than regenerating history:

```bash
.venv/Scripts/python scripts/snapshot.py --save      # after a good rebuild
.venv/Scripts/python scripts/snapshot.py --restore   # back to it, ~1 minute
```

`scripts/demo_reset.py` is the slow path and is rarely what you want: it
generates a month of new history through the real API and takes about three
hours (see "Give it something to look at" below). The sections that follow set
that up from nothing, or explain what each piece does.

### 1. PostgreSQL

Any PostgreSQL 16 instance will do. If the machine has none installed, the
portable binary distribution needs no installer and no administrator rights:

```bash
# download and unzip postgresql-16.x-windows-x64-binaries.zip, then
pgsql/bin/initdb -D <datadir> -U postgres --auth-local=trust \
                 --auth-host=scram-sha-256 --pwfile=<pwfile> -E UTF8
pgsql/bin/pg_ctl -D <datadir> -l pg.log -o "-p 55432" start
```

Set `RISKRADAR_PG_SUPERUSER_PASSWORD` in the environment if it is not the
default used by `scripts/migrate.py`.

### 2. Configure

```bash
cp .env.example .env      # then fill in the pepper and the passwords
```

`RISKRADAR_HMAC_PEPPER` is the tokenisation secret. It is one-way, so leaking it
exposes nothing — but **changing it re-tokenises the world** and orphans every
behavioural baseline.

### 3. Install and migrate

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python scripts/migrate.py     # creates roles, database, schema
.venv/Scripts/python scripts/seed.py        # users, API key, ruleset, thresholds
npm install --prefix frontend
```

`seed.py` prints the development logins and the `otpauth://` URIs. TOTP is on for
every account; `scripts/totp.py <email>` prints a current code for rehearsal.

### 4. Run

Four processes:

```bash
.venv/Scripts/python scripts/run_api.py         # http://127.0.0.1:8000
.venv/Scripts/python scripts/run_workers.py 3   # scoring workers
.venv/Scripts/python scripts/run_clocks.py       # CBN clock breaches and 24h flag expiry, every 60s (D71, D73)
npm run dev --prefix frontend                   # http://localhost:5173
```

### 5. Give it something to look at

```bash
cd simulator
# a week of behaviour, with the last 14 hours alerting into the case queue
python -m riskradar_sim --seed 424242 history --profile demo --alerting-tail-hours 14
# a live feed while you demo: it continues the demo bank's own customers (D88a),
# incidents unfold in their own time (D87); --incident-speed 10 compresses them
python -m riskradar_sim stream --rate 2
# another bank flags three of your customers' BVNs on the industry watch-list (D75)
python -m riskradar_sim industry-flag --count 3
```

`history` and `stream` also write `fixtures/core_identities.csv`, the simulated
core's customer file, which the API reads to learn each customer's BVN. The
identity and industry adapters are chosen in `.env` (see `.env.example`); the
real Finacle and NIBSS adapters wait, visibly, until configured.

### Running it for real

`deploy/README.md` is the production runbook: settings, start order, the
health and readiness probes, rate limits and backpressure (D87), operating the
alert budget (D86), drift monitoring (D88), retraining on the desk's own
outcomes (D89), backups and upgrades. `docs/api/openapi.json` is the full API
description; `docs/dashboard-api-contract.html` maps it to screens.

### 6. Test it on somebody else's transactions (D81)

Any transaction file (CSV, TSV, JSON lines; Parquet or Excel with pyarrow or
openpyxl) can be run through the same measurements, rules and policy the live
system uses:

```bash
# draft a column mapping, then read it, correct it and set reviewed: true
python ml/evaluate_dataset.py suggest path/to/transactions.csv
python ml/evaluate_dataset.py run path/to/transactions.csv --mapping path/to/transactions.mapping.yaml
```

It writes an HTML report, JSON and the alert list to `ml/artifacts/datasets/`:
which of the 26 measurements and 13 rules the data can feed at all, then rules
alone, the shipped model, a model retrained on the older part of the file, an
anomaly score that never sees a label and amount alone, all at the same alert
budget, with 95% ranges; and, when the file labels fraud types, a test that
hides each type from training in turn. Columns somebody else computed (scores,
risk, flags) are never used as inputs. Reviewed mappings for PaySim and the
ElectricSheep Nigerian dataset are in `ml/datasets/`.

---

## Layout

```
backend/
  riskradar/
    api/          FastAPI app, routers, request/response contracts
    features/     THE SHARED FEATURE PACKAGE — training and serving both use it
    rules/        six rules emitting named signals, never points
    policy/       the only layer that decides anything
    model/        registry, loading, promotion, ablation attributions
    worker/       queue claiming and the scoring loop
    cases/        alert-to-case correlation
    audit/        hash-chained append-only log
    security/     tokenisation, Argon2id, TOTP, sessions, RBAC
  migrations/     numbered SQL, applied by scripts/migrate.py
  tests/          54 tests, run against real PostgreSQL
frontend/         React dashboard — queue, case detail, metrics, administration
simulator/        latent-process transaction generator; a pure external client
ml/               corpus, training, evaluation, artifacts
scripts/          migrate · seed · run · load test · threshold derivation
docs/             PRD, decision log, core banking reference, QA report
```

---

## The four things worth knowing before reading the code

**One feature package, two data-access paths.** `riskradar.features` holds pure
functions taking a transaction plus its history. Training feeds them a dataframe;
the worker feeds them a Postgres query. `tests/test_train_serve_equality.py`
asserts all three access paths produce identical vectors — the highest-value test
in the suite, and one that cannot be retrofitted once the paths have drifted.

**The decision is written before the alert.** Always, for every scored
transaction. It carries the score, the band, the model version, the ruleset
version, the threshold version and the full feature snapshot, which is what makes
a decision reproducible months later.

**Postgres is the queue.** `UPDATE … WHERE transaction_id IN (SELECT … FOR UPDATE
SKIP LOCKED)` leases a batch and commits; each transaction is then scored in its
own short transaction. No broker, one durability story — and short transactions,
which is what lets workers actually run in parallel.

**The audit log cannot be rewritten by the process that writes it.** The
application database role holds `INSERT` and `SELECT` on `audit_log` and nothing
else. That is a grant, not a convention, and `tests/test_security.py` proves it
against the live database.

---

## Testing

```bash
.venv/Scripts/python -m pytest              # 54 tests
.venv/Scripts/python scripts/load_test.py --tps 50 --seconds 60      # NFR-001
.venv/Scripts/python scripts/load_test.py --burst --tps 250 --seconds 60  # NFR-002
.venv/Scripts/python ml/evaluate.py         # held-out typology, model alone
.venv/Scripts/python ml/evaluate_system.py  # held-out typology, full system
```

The suite runs against real PostgreSQL rather than a mock or SQLite: `SKIP
LOCKED`, `LISTEN`/`NOTIFY` and the `audit_log` grant have no meaning on any other
engine, so a suite that avoided Postgres would be testing a different system than
the one that ships.

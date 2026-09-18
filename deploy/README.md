# Running Risk Radar in production

This is the runbook for the backend: the API, the scoring workers and the clock
sweep, over one PostgreSQL 16 database. The same three processes run natively
(`scripts/run_api.py`, `scripts/run_workers.py N`, `scripts/run_clocks.py`) or
from one container image (`deploy/Dockerfile`, `deploy/docker-compose.yml`).
Risk Radar is **advisory** (D7): it decides and explains, and the bank's switch
enforces.

## Before the first start

1. Copy `.env.example` to `.env`. Set real values for the two database role
   passwords, `RISKRADAR_HMAC_PEPPER` (long and random; changing it later
   orphans every behavioural baseline), and `RISKRADAR_PG_SUPERUSER_PASSWORD`.
2. Set `RISKRADAR_ENV=production` and `RISKRADAR_CORS_ORIGINS` to the console's
   origin. Production turns on secure cookies, HSTS and JSON logs.
3. `python scripts/migrate.py` creates the roles and database and applies the
   numbered migrations. `migrate.py` bootstraps roles with development
   passwords; in production, `ALTER ROLE riskradar_app PASSWORD ...` and the
   same for `riskradar_migrate` to match `.env`.
4. `python scripts/seed.py` creates the users, rules and a placeholder
   threshold set. It also creates a **development API key that is public**.
   Create the bank's key (`POST /v1/admin/api-keys?name=switch`), then revoke
   the development one (`DELETE /v1/admin/api-keys/{id}`). In production,
   readiness stays failed until you do.
5. Register and promote a model (`ml/train.py --final --promote`, or
   `POST /v1/admin/models/promote`), then derive thresholds from real traffic
   (below).

## Start order and probes

| Process | Command | Probe |
|---|---|---|
| API | `python scripts/run_api.py` | `GET /health/live` (process up, no DB call) and `GET /health/ready` (503 with the failing checks) |
| Scoring workers | `python scripts/run_worker.py`, any number of copies | Heartbeats in `worker_heartbeats`; readiness needs one seen within `RISKRADAR_WORKER_STALE_SECONDS` |
| Clock sweep | `python scripts/run_clocks.py 60` | One copy is enough; breaches are recorded once however many run |

Readiness requires the database, an active ruleset and threshold set, a live
worker, and live queue lag under two minutes. A missing model is reported but
not fatal: scoring runs on rules only and raises `MODEL_UNAVAILABLE` (FR-017).

Sizing, measured on one 8-core machine with the whole stack on it (D87a): three
API processes (`RISKRADAR_API_WORKERS=3`) and four scoring workers held 50
payments a second at p95 210 ms end to end, and accepted a 231-a-second burst
with nothing lost. Workers run one model thread each (`run_worker.py` sets it)
and warm the model before taking work; add workers, not threads, for throughput.

Put a TLS-terminating proxy in front of the API. It trusts `X-Forwarded-*`
headers in production. Each response carries `X-Request-ID`; quote it when
reporting a problem, because every log line for that request carries it too.

## Protecting the service (D87)

- **Rate limits.** Each API key has two token buckets: live traffic
  (`RISKRADAR_INGEST_RATE`/`_BURST`) and replayed history (`RISKRADAR_REPLAY_*`).
  A caller over its rate gets `429` with `Retry-After`. The buckets are per
  process; behind several API processes, divide the rate or limit at the
  gateway.
- **Backpressure.** Every ingestion response carries `X-RiskRadar-Queue-Depth`.
  Above `RISKRADAR_QUEUE_SOFT_LIMIT` live payments waiting, responses add
  `X-RiskRadar-Backpressure: slow-down`. Above `RISKRADAR_QUEUE_HARD_LIMIT` new
  live work is refused with `503` and `Retry-After`. Replayed history pauses at
  `RISKRADAR_REPLAY_QUEUE_LIMIT`.
- **Priority.** Live payments are scored before replayed history. A backfill
  never delays today's decisions.
- **Clock skew.** A payment dated more than `RISKRADAR_MAX_CLOCK_SKEW_SECONDS`
  ahead of the server is refused (422 and a dead-letter row).
- **Idempotency.** A repeated `transaction_ref` is a no-op returning the
  original result, so retries after a timeout are always safe.

`simulator/riskradar_sim/client.py` is the reference client: paced, it halves
its speed on `slow-down`, waits out `429`/`503`, and counts every refusal.

## The alert budget at run time (D86)

Thresholds are derived backwards from the budget (`alert_budget_per_day`, 75).
On top of that, every alert passes the budget guard:

- It counts against the bank's local day (UTC+1), in `alert_budget_days`.
- Once the day's budget is spent, or the hour has used
  `ceil(budget × hourly_burst ÷ 24)`, a discretionary alert is **deferred**.
  It waits in `alert_deferrals`, most serious first, and is raised as soon as
  there is room. Workers check every 30 seconds.
- Veto rules (sanctions, known mule) and machine actions are never deferred;
  they are counted. If they alone overrun the day, the alarm is
  `ALERT_BUDGET_OVERRUN`: retune those rules.
- A deferral still waiting after `alert_deferral_hours` (24) expires with
  `ALERTS_EXPIRED_UNREVIEWED`. That number is the one to act on.

Operating it:

| Question | Endpoint |
|---|---|
| Where is today against the budget? | `GET /v1/budget` |
| What is waiting? | `GET /v1/budget/deferred` |
| Pull one in now (lead) | `POST /v1/budget/deferred/{id}/release` |
| Do the thresholds still fit the traffic? | `GET /v1/budget/calibration` (verdict FITS / OVER / UNDER) |
| Re-derive and publish thresholds (admin) | `POST /v1/admin/thresholds/derive` with `{"publish": true}` |
| Change the budget or pacing (admin) | `PUT /v1/admin/budget` (audited, reason required) |

After promoting a new model, always re-derive thresholds: a new model scores
on a new scale.

## Watching the model (D88, D89)

- `GET /v1/metrics/drift`: PSI of the score and of each model input, recent
  day against the week before, plus each rule's firing rate. Readings: under
  0.10 stable, above 0.25 shifted. A rule that went `SILENT` or `SPIKE`d is
  listed under `needs_attention`.
- `python ml/retrain_from_outcomes.py` retrains on the desk's closed cases
  using the stored feature snapshots, and compares challenger with champion on
  the newest quarter. It refuses with fewer than 30 confirmed frauds.
  `--register` records the challenger inactive; promotion is an audited admin
  action.

## Backups and upgrades

- Back up PostgreSQL (for example `pg_dump -Fc`, or WAL archiving for
  point-in-time recovery). It holds the queue, the decisions and the
  hash-chained audit log. `GET /v1/admin/audit/verify` checks the chain after
  a restore.
- Upgrades: stop the workers, then `python scripts/migrate.py`, then restart
  the API **and** the workers. Prepared statements cached before a
  column-type migration fail with "cached plan must not change result type"
  until the process restarts.

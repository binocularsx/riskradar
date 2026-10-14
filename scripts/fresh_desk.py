"""Start the demo on a fresh desk. `python scripts/up.py --fresh`

Why this exists
---------------
`up.py` reuses the database on disk, so the desk carries on from wherever the
last demo left it: cases opened days ago, every one of them past its SLA (15
minutes to 24 hours, by risk level), and a queue that only fills again as the
live feed trickles in. A restored snapshot is no better — it is frozen at the
day it was saved, so it is just as stale.

A fresh start does four things, in this order:

1. **Clone the base.** `riskradar_fresh_base` is a ready database in the same
   Postgres cluster: a month of history, accounts, users, model and thresholds,
   with the open queue already removed (closed cases stay — a desk with no past
   looks like it opened this morning). Cloning it is a file copy, not a
   restore. Whatever happened in the last demo is gone.
2. **Move its clock to now.** Every timestamp moves forward by the same amount,
   so the newest payment in the history was a moment ago and every account's
   behaviour is current. Relative times — ages, gaps, windows, clocks — are
   unchanged. The big tables move in parallel. The audit chain hashes each
   entry's time, so it is re-sealed and then verified; the triggers that keep
   history append-only are bypassed for these superuser sessions only. A
   failure part-way is harmless: the next fresh start clones the base again.
3. **Give today its whole alert budget** back, for the warm-up and the feed.
4. **Warm up** (once the API and workers are running): a fast, fraud-rich burst
   of the live feed, incidents compressed a hundredfold, with the alert
   budget's hourly pacing lifted (D86) until the queue holds a full load of
   brand-new cases. Pacing is then put back and the ordinary feed takes over.

The base is built from `fixtures/fresh-base.dump`, the portable copy of it.
Replace both from the current database (stop the demo first):

    python scripts/fresh_desk.py --save-base

or rebuild the cluster copy alone from the dump (e.g. on a new machine):

    python scripts/fresh_desk.py --build-base

It is synthetic simulator traffic only (D28, D101a), like every snapshot.
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import snapshot  # noqa: E402
from riskradar.audit import chain  # noqa: E402
from riskradar.config import settings  # noqa: E402

PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
LOGS = REPO_ROOT / "logs"
BASE_DB = "riskradar_fresh_base"
BASE_DUMP = snapshot.FIXTURES / "fresh-base.dump"
# The simulated support team remembers which restriction orders it has already
# seen (by id). It is saved with the base so it matches the cloned database.
BASE_SUPPORT_STATE = snapshot.FIXTURES / "fresh-base.support-state.json"
SUPPORT_STATE = LOGS / "sim_support_state.json"
VICTIMS = LOGS / "sim_fraud_victims.jsonl"

# Real calendar dates: moving them would put Independence Day in the wrong week.
NOT_SHIFTED = {"public_holidays"}
# Emptied in the base: they describe processes, logins and the last demo's
# conversations, not history.
EMPTIED = ("sessions", "worker_heartbeats", "scoring_queue", "dead_letter", "notifications")


def say(step: str, detail: str) -> None:
    print(f"  {step:<32}{detail}", flush=True)


def _superuser(dbname: str, **kw) -> psycopg.Connection:
    s = settings()
    return psycopg.connect(
        host=s.db_host, port=s.db_port, dbname=dbname,
        user=os.environ.get("RISKRADAR_PG_SUPERUSER", "postgres"),
        password=snapshot._superuser_env()["PGPASSWORD"],
        row_factory=dict_row, **kw,
    )


def _base_exists() -> bool:
    with _superuser("postgres") as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (BASE_DB,)).fetchone() is not None


def _drop(dbname: str) -> None:
    # A template cannot be dropped until it is an ordinary database again.
    with _superuser("postgres", autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone():
            conn.execute(f'ALTER DATABASE "{dbname}" IS_TEMPLATE false')
    snapshot._run("dropdb", [*snapshot._conn_args(), "--if-exists", "--force", dbname])


# ---------------------------------------------------------------------------
# The base
# ---------------------------------------------------------------------------


def _clear_carried(conn: psycopg.Connection) -> int:
    """Remove the open queue. Triggers are off, so ON DELETE CASCADE (itself
    a trigger) is too: a case's dependants are removed explicitly."""
    conn.execute("CREATE TEMP TABLE carried ON COMMIT DROP AS SELECT id FROM cases WHERE state <> 'CLOSED'")
    conn.execute("""
        DELETE FROM directives WHERE acknowledged_at IS NULL AND transaction_id IN
          (SELECT transaction_id FROM alerts WHERE case_id IN (SELECT id FROM carried))
    """)
    # Waiting deferrals would be released later into cases that look old.
    conn.execute("DELETE FROM alert_deferrals WHERE resolved_at IS NULL OR alert_id IN "
                 "(SELECT id FROM alerts WHERE case_id IN (SELECT id FROM carried))")
    conn.execute("DELETE FROM restriction_outbox WHERE order_id IN "
                 "(SELECT id FROM restriction_orders WHERE case_id IN (SELECT id FROM carried))")
    for child in ("alerts", "case_actions", "case_notes", "clock_breaches", "support_reports",
                  "restriction_orders", "fraud_submissions"):
        conn.execute(f"DELETE FROM {child} WHERE case_id IN (SELECT id FROM carried)")
    conn.execute("UPDATE subject_watchlist SET case_id = NULL WHERE case_id IN (SELECT id FROM carried)")
    removed = conn.execute("DELETE FROM cases WHERE id IN (SELECT id FROM carried)").rowcount
    for table in EMPTIED:
        conn.execute(f"DELETE FROM {table}")
    return removed


def build_base() -> None:
    """The cluster copy, from the dump: slow (minutes), done once."""
    if not BASE_DUMP.exists():
        raise SystemExit(f"no fresh-desk base at {BASE_DUMP} — make one with "
                         "`python scripts/fresh_desk.py --save-base`")
    started = time.time()
    say("fresh-desk base", f"building {BASE_DB} from {BASE_DUMP.name} (once; a few minutes)")
    _drop(BASE_DB)
    snapshot._run("createdb", [*snapshot._conn_args(), "-O", "riskradar_migrate", BASE_DB])
    snapshot._run("pg_restore", [*snapshot._conn_args(), "-d", BASE_DB, "-j", "4", str(BASE_DUMP)])
    with _superuser(BASE_DB) as conn:
        conn.execute("SET session_replication_role = replica")
        removed = _clear_carried(conn)
        conn.commit()
    with _superuser(BASE_DB, autocommit=True) as conn:
        conn.execute("VACUUM ANALYZE")
    with _superuser("postgres", autocommit=True) as conn:
        conn.execute(f'ALTER DATABASE "{BASE_DB}" IS_TEMPLATE true')
    say("fresh-desk base", f"ready in {time.time() - started:.0f}s ({removed} open cases left out)")


def save_base() -> None:
    """The current database becomes the base: the dump, then the cluster copy."""
    snapshot.save(BASE_DUMP)
    if SUPPORT_STATE.exists():
        shutil.copyfile(SUPPORT_STATE, BASE_SUPPORT_STATE)
    build_base()


def clone_base(target: str | None = None) -> None:
    """Step 1. The caller has stopped everything that holds `target` open."""
    target = target or settings().db_name
    if not _base_exists():
        build_base()
    started = time.time()
    _drop(target)
    # FILE_COPY copies the base's files at a checkpoint — seconds for gigabytes,
    # where the default strategy writes every block through the WAL.
    snapshot._run("createdb", [*snapshot._conn_args(), "-T", BASE_DB, "-O", "riskradar_migrate",
                               "-S", "file_copy", target])
    say("database", f"fresh copy of the base in {time.time() - started:.0f}s")


def reset_sim_state() -> None:
    """The simulator's own memory, matched to the base."""
    if BASE_SUPPORT_STATE.exists():
        shutil.copyfile(BASE_SUPPORT_STATE, SUPPORT_STATE)
    else:
        SUPPORT_STATE.unlink(missing_ok=True)
    # Victims of the last demo's incidents: their payments are not in the base.
    VICTIMS.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Moving the clock
# ---------------------------------------------------------------------------


def _time_columns(conn: psycopg.Connection) -> dict[str, list[tuple[str, str]]]:
    rows = conn.execute(
        """
        SELECT c.table_name, c.column_name, c.data_type
          FROM information_schema.columns c
          JOIN information_schema.tables t USING (table_schema, table_name)
         WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
           AND c.data_type IN ('timestamp with time zone', 'date')
         ORDER BY 1, 2
        """
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        if r["table_name"] not in NOT_SHIFTED and r["table_name"] not in EMPTIED:
            out.setdefault(r["table_name"], []).append((r["column_name"], r["data_type"]))
    return out


def _shift_table(dbname: str, table: str, cols: list[tuple[str, str]], shift, days: int) -> float:
    started = time.time()
    with _superuser(dbname) as conn:
        # Skips triggers, including the append-only guards, for this session only.
        conn.execute("SET session_replication_role = replica")
        conn.execute("SET synchronous_commit = off")
        conn.execute("SET maintenance_work_mem = '512MB'")
        # On a big table every moved row also rewrites an entry in every index
        # — row by row, nine indexes on transactions. Dropping the plain
        # indexes and building them again afterwards is several times faster.
        # Constraint indexes stay (primary and unique keys). One transaction,
        # so a failure rolls the drops back too.
        rebuild = conn.execute(
            """
            SELECT i.indexrelid::regclass::text AS name, pg_get_indexdef(i.indexrelid) AS ddl
              FROM pg_index i
             WHERE i.indrelid = %s::regclass
               AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
               AND pg_total_relation_size(i.indrelid) > 100 * 1024 * 1024
            """,
            (f'"{table}"',),
        ).fetchall()
        for ix in rebuild:
            conn.execute(f"DROP INDEX {ix['name']}")
        sets = []
        for col, kind in cols:
            if kind == "date":
                # Two steps: a whole-table `day = day + n` collides with the
                # next day's primary key part-way through.
                conn.execute(f'UPDATE "{table}" SET "{col}" = "{col}" + 100000')
                sets.append(f'"{col}" = "{col}" - 100000 + %(days)s')
            else:
                sets.append(f'"{col}" = "{col}" + %(shift)s')
        conn.execute(f'UPDATE "{table}" SET {", ".join(sets)}', {"shift": shift, "days": days})
        for ix in rebuild:
            conn.execute(ix["ddl"])
        conn.commit()
    return time.time() - started


def _reseal_audit_chain(conn: psycopg.Connection) -> int:
    """Recompute every hash after the times moved, in chain order."""
    prev = chain.GENESIS
    buf = io.StringIO()
    n = 0
    with conn.cursor(name="audit_walk") as cur:
        cur.execute("SELECT id, occurred_at, actor_user_id, action, object_type, object_id, "
                    "from_state, to_state, payload FROM audit_log ORDER BY id")
        for r in cur:
            body = chain.canonical_payload(r["occurred_at"], r["actor_user_id"], r["action"],
                                           r["object_type"], r["object_id"], r["from_state"],
                                           r["to_state"], r["payload"])
            digest = chain._chain_hash(prev, body)
            buf.write(f"{r['id']}\t{prev}\t{digest}\n")
            prev = digest
            n += 1
    conn.execute("CREATE TEMP TABLE resealed (id bigint PRIMARY KEY, prev_hash text, hash text) ON COMMIT DROP")
    with conn.cursor().copy("COPY resealed (id, prev_hash, hash) FROM STDIN") as copy:
        copy.write(buf.getvalue())
    conn.execute("UPDATE audit_log a SET prev_hash = r.prev_hash, hash = r.hash FROM resealed r WHERE r.id = a.id")
    return n


def rebase(target: str | None = None) -> None:
    """Steps 2 and 3, on a fresh clone."""
    target = target or settings().db_name
    started = time.time()
    with _superuser(target) as conn:
        # The chain is verified the way the API reads it, so hash in its zone:
        # the database default, which an app session also gets.
        zone = conn.execute("SHOW TimeZone").fetchone()["TimeZone"]
        latest = conn.execute(
            "SELECT max(ingested_at) AS at, now() - max(ingested_at) AS shift, "
            "current_date - max(ingested_at)::date AS days FROM transactions"
        ).fetchone()
        if latest["at"] is None:
            raise SystemExit("the base has no transactions — is it the right database?")
        tables = _time_columns(conn)
    shift, days = latest["shift"], latest["days"]

    # Biggest first, so the longest update starts at once.
    with _superuser(target) as conn:
        sizes = {r["relname"]: r["n"] for r in conn.execute(
            "SELECT relname, pg_total_relation_size(relid) AS n FROM pg_stat_user_tables").fetchall()}
    order = sorted(tables, key=lambda t: -sizes.get(t, 0))
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {t: pool.submit(_shift_table, target, t, tables[t], shift, days) for t in order}
        took = {t: f.result() for t, f in futures.items()}
    slowest = max(took, key=took.get)
    say("clock moved forward", f"{shift} in {time.time() - started:.0f}s (longest: {slowest}, {took[slowest]:.0f}s)")

    with _superuser(target) as conn:
        conn.execute(f"SET TimeZone = '{zone}'")
        conn.execute("SET session_replication_role = replica")
        sealed = _reseal_audit_chain(conn)
        # Today starts with its whole alert budget, for the warm-up and the feed.
        conn.execute("DELETE FROM alert_budget_days WHERE day >= current_date")
        conn.execute("SET session_replication_role = origin")
        result = chain.verify(conn)
        if not result["ok"]:
            raise SystemExit(f"audit chain does not verify after the move: {result}")
        conn.commit()
    say("audit chain", f"{sealed:,} entries re-sealed and verified")


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------


def _set_hourly_burst(value: float, reason: str) -> float:
    import demo_reset

    return demo_reset.set_hourly_burst(value, reason)


def _open_cases() -> int:
    with psycopg.connect(settings().app_dsn) as conn:
        return conn.execute("SELECT count(*) FROM cases WHERE state <> 'CLOSED'").fetchone()[0]


def warm_up(target_cases: int, max_minutes: float = 8, base_url: str | None = None) -> None:
    """Step 4: the API and workers must be running."""
    import demo_reset

    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "simulator"))
    LOGS.mkdir(exist_ok=True)
    burst = _set_hourly_burst(24.0, "fresh desk: warm-up fills the queue in minutes")
    started = time.time()
    url = ["--base-url", base_url] if base_url else []
    try:
        with open(LOGS / "warmup.log", "wb") as log:
            # Fraud-rich and compressed a hundredfold: a three-hour takeover
            # plays out in two minutes, and its setup is minutes old, not
            # hours. Only the warm-up runs like this; the live feed after it is
            # the ordinary one.
            feed = subprocess.Popen(
                [str(PYTHON), "-u", "-m", "riskradar_sim", *url, "stream", "--rate", "20",
                 "--max-rate", "80", "--incident-probability", "0.05", "--incident-speed", "100",
                 "--minutes", str(max_minutes)],
                cwd=str(REPO_ROOT / "simulator"), env=env, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            try:
                while feed.poll() is None:
                    n = _open_cases()
                    print(f"\r  {'warm-up':<32}{n} of {target_cases} cases …", end="", flush=True)
                    if n >= target_cases:
                        break
                    time.sleep(2)
            finally:
                if feed.poll() is None:
                    feed.terminate()
                    feed.wait(timeout=30)
        print()
        demo_reset.wait_for_drain("warm-up", timeout_s=600)
    finally:
        _set_hourly_burst(burst, "fresh desk: warm-up done; live pacing restored")
    # The warm-up is setup, like loading history: it is far richer in fraud
    # than any real day (hundreds of machine actions in minutes, against ~17 a
    # day measured, D86), so its spend would put the live day over budget
    # before it starts and hold back every alert after it. The live day starts
    # its ledger from zero; what the warm-up held back is dropped.
    with _superuser(settings().db_name) as conn:
        conn.execute("SET session_replication_role = replica")
        dropped = conn.execute("DELETE FROM alert_deferrals WHERE resolved_at IS NULL AND deferred_at >= %s",
                               (datetime.fromtimestamp(started, timezone.utc),)).rowcount
        conn.execute("DELETE FROM notifications WHERE case_id IS NULL AND created_at >= %s",
                     (datetime.fromtimestamp(started, timezone.utc),))
        conn.execute("DELETE FROM alert_budget_days WHERE day >= current_date")
        conn.commit()
    say("warm-up", f"{_open_cases()} new cases in {time.time() - started:.0f}s; "
                   f"today's budget starts unspent ({dropped:,} warm-up alerts not carried)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Prepare the fresh-desk base")
    ap.add_argument("--save-base", action="store_true",
                    help="make the current database the base (dump + cluster copy); stop the demo first")
    ap.add_argument("--build-base", action="store_true",
                    help="rebuild the cluster copy from fixtures/fresh-base.dump")
    args = ap.parse_args()
    print("\nRisk Radar - fresh desk\n")
    if args.save_base:
        save_base()
    elif args.build_base:
        build_base()
    else:
        print("  a fresh start is part of the launcher:  python scripts/up.py --fresh")
    print()

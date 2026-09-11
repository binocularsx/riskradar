"""Build a believable fraud desk from scratch.

    python scripts/demo_reset.py --stage schema   # API must be STOPPED
    python scripts/demo_reset.py --stage data     # API must be RUNNING

Why this exists
---------------
The first demo database had 3,218 open cases, 1,948 of them past their SLA, and
a billion naira of exposure. No fraud desk on earth looks like that, and a
screen showing it cannot be judged — every design looks bad when the data is
nonsense.

The cause was an ordering mistake, not a rendering one. Thresholds were left at
their placeholder values while a fraud-rich demo feed ran against them, so
roughly one transaction in five became an alert. The fix is to do it in the
order a real deployment would:

1. Load history with alerting **off**. This gives every account a behavioural
   baseline and gives us a distribution of scores to reason about.
2. **Derive the thresholds from that distribution** against the alert budget
   (D11d) — 120 alerts a day for a three-analyst desk.
3. *Then* run a short window with alerting on. The case count that falls out is
   whatever the budget implies, which is the whole point of having a budget.
4. Work a few cases, so the operations view has outcomes in it and the desk does
   not look like it opened this morning.

The result is a desk with a few dozen open cases — which is what a three-analyst
team actually carries.
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
# Simulator truth for the alerting window. Git-ignored with the rest of ml/data.
LABELS = REPO_ROOT / "ml" / "data" / "demo-tail-labels.jsonl"


def run(cmd: list[str], cwd: Path | None = None, quiet: bool = False) -> str:
    result = subprocess.run(
        cmd, cwd=cwd or REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"failed: {' '.join(str(c) for c in cmd)}")
    if not quiet:
        tail = result.stdout.strip().splitlines()[-4:]
        for line in tail:
            print(f"    {line}")
    return result.stdout


# ---------------------------------------------------------------------------
# Stage 1 — schema, users, and the trained model. No API needed.
# ---------------------------------------------------------------------------


def stage_schema() -> None:
    print("[1/4] dropping and rebuilding the database")
    run([str(PYTHON), "scripts/migrate.py", "--reset"])
    run([str(PYTHON), "scripts/migrate.py"])

    print("[2/4] seeding users, API key, rules and placeholder thresholds")
    run([str(PYTHON), "scripts/seed.py"])

    print("[3/4] registering the trained model")
    register_trained_model()

    print("[4/4] disabling MFA on the analyst account for demo convenience")
    disable_analyst_mfa()

    print("\nschema stage done. Start the API, then run --stage data.")


def register_trained_model() -> None:
    """Point the fresh database at the model artefact already on disk.

    Retraining takes minutes and produces the same file; the artefact is
    content-hashed, so re-registering it is exactly as trustworthy as training
    it again (D15a).
    """
    import hashlib
    import json

    import psycopg

    from riskradar.config import settings
    from riskradar.features.spec import FEATURE_SPEC_VERSION

    artifacts = sorted((REPO_ROOT / "ml" / "artifacts").glob("riskradar-gbm-*.joblib"))
    if not artifacts:
        print("    no trained model found — the stub will be used until you run ml/train.py")
        return
    artifact = artifacts[-1]
    version = artifact.stem.replace("riskradar-gbm-", "")

    reports = sorted((REPO_ROOT / "ml" / "artifacts").glob(f"evaluation-{version}.json"))
    metrics = json.loads(reports[-1].read_text()) if reports else {}

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("UPDATE model_versions SET is_active = false WHERE is_active")
        conn.execute(
            """
            INSERT INTO model_versions
                (name, version, artifact_hash, artifact_path, feature_spec_version,
                 calibration, metrics, trained_at, is_active, promoted_at)
            VALUES ('riskradar-gbm', %s, %s, %s, %s, 'isotonic', %s, now(), true, now())
            ON CONFLICT (name, version) DO UPDATE SET is_active = true
            """,
            (
                version,
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                artifact.relative_to(REPO_ROOT).as_posix(),
                FEATURE_SPEC_VERSION,
                json.dumps(metrics),
            ),
        )
        conn.commit()
    print(f"    active model: riskradar-gbm:{version}")


def disable_analyst_mfa() -> None:
    """D12a makes MFA *default-on* for ANALYST, mandatory only for lead and admin.

    Turning it off for the demo analyst is inside the decision, not a bypass of
    it — and the change is audited like any other.
    """
    import psycopg

    from riskradar.audit import chain
    from riskradar.config import settings

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        sys_uid = conn.execute("SELECT id FROM users WHERE is_system LIMIT 1").fetchone()["id"]
        conn.execute(
            "UPDATE users SET totp_enabled = false WHERE email = 'analyst@riskradar.local'"
        )
        chain.append(
            conn, actor_user_id=sys_uid, action="USER_MFA_DISABLED",
            object_type="user", object_id="analyst@riskradar.local",
            from_state="true", to_state="false",
            payload={"reason": "demo convenience; D12a permits default-on for ANALYST"},
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Stage 2 — data, in the order a real deployment would do it.
# ---------------------------------------------------------------------------


def wait_for_drain(label: str, timeout_s: float = 1800) -> None:
    import psycopg

    from riskradar.config import settings

    started = time.perf_counter()
    while time.perf_counter() - started < timeout_s:
        with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
            depth = conn.execute("SELECT count(*) AS n FROM scoring_queue").fetchone()["n"]
        if depth == 0:
            print(f"    {label}: scored in {time.perf_counter() - started:.0f}s")
            return
        print(f"\r    {label}: {depth} left to score …", end="", flush=True)
        time.sleep(2)
    raise SystemExit(f"{label}: queue did not drain — are the workers running?")


def stage_data(args: argparse.Namespace) -> None:
    sim = REPO_ROOT / "simulator"

    print("[1/5] loading history with alerting OFF")
    print("      (baselines need history; nobody needs paging about last week)")
    run(
        [str(PYTHON), "-u", "-m", "riskradar_sim", "--seed", str(args.seed),
         "history", "--profile", "demo", "--batch-size", "500",
         "--alerting-tail-hours", "0"],
        cwd=sim,
    )
    wait_for_drain("history")

    print("\n[2/5] deriving thresholds from that traffic against the alert budget")
    run([str(PYTHON), "-u", "scripts/derive_thresholds.py", "--publish"])

    print("\n[3/5] replaying the last hours with alerting ON")
    print("      whatever case count falls out is what the budget implies")
    run(
        [str(PYTHON), "-u", "-m", "riskradar_sim", "--seed", str(args.seed + 1),
         "history", "--profile", "demo", "--batch-size", "300",
         "--alerting-tail-hours", str(args.alerting_hours), "--only-tail",
         "--labels-out", str(LABELS)],
        cwd=sim,
    )
    wait_for_drain("alerting window")

    print("\n[4/5] working a few cases so the desk has history")
    seed_worked_cases(args.seed)

    print("\n[5/5] summary")
    summarise()


def seed_worked_cases(seed: int) -> None:
    """Give the desk a plausible past.

    A queue where every case is untouched looks like a system switched on ten
    minutes ago; a queue where every clock has run out looks like a desk nobody
    staffs. A real three-analyst desk works its cases roughly in the order the
    clocks demand, so:

    * every case whose response clock has already run out has been **worked and
      closed**, inside its target time, the way a staffed desk would have;
    * about a tenth of the rest are in progress with an analyst;
    * the recent ones are waiting, which is the worklist the demo shows.

    Outcomes follow the simulator's truth for each case (D66a), not a dice roll.
    A random mix put the false-alarm rate wherever the dice landed and would
    poison the labels if anyone ever retrained on them (D13c). A case with no
    truth on record is honestly marked inconclusive.

    Everything goes through the real audit trail, so the history is genuine
    rather than painted on.
    """
    import json

    import psycopg

    from riskradar.audit import chain
    from riskradar.cases import triage
    from riskradar.config import settings

    rng = random.Random(seed)

    truth: dict[str, bool] = {}
    if LABELS.exists():
        for line in LABELS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                truth[r["transaction_ref"]] = bool(r["is_fraud"])
    else:
        print(f"    no truth file at {LABELS}; outcomes will be inconclusive")

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        users = {
            u["email"]: u["id"]
            for u in conn.execute(
                "SELECT id, email FROM users WHERE role IN ('ANALYST','FRAUD_OPS_LEAD')"
            ).fetchall()
        }
        analyst = users.get("analyst@riskradar.local")
        lead = users.get("lead@riskradar.local")

        open_cases = conn.execute(
            """
            SELECT c.id, c.risk_level,
                   EXTRACT(EPOCH FROM (now() - c.opened_at)) / 60.0 AS age_minutes,
                   array_agg(t.transaction_ref) AS refs
              FROM cases c
              JOIN alerts a ON a.case_id = c.id
              JOIN transactions t ON t.id = a.transaction_id
             WHERE c.state = 'OPEN'
             GROUP BY c.id
             ORDER BY c.id
            """
        ).fetchall()
        if not open_cases:
            print("    no cases to work — check the alerting window")
            return

        def outcome_for(case: dict) -> str:
            known = [truth[r] for r in case["refs"] if r in truth]
            if not known:
                return "INCONCLUSIVE"
            return "CONFIRMED_FRAUD" if any(known) else "FALSE_POSITIVE"

        def target(case: dict) -> float:
            return float(triage.SLA_MINUTES.get(case["risk_level"], 240))

        overdue = [c for c in open_cases if float(c["age_minutes"]) > target(c)]
        waiting = [c for c in open_cases if float(c["age_minutes"]) <= target(c)]
        rng.shuffle(waiting)
        in_review = waiting[: max(2, len(waiting) // 10)]

        tally = {"CONFIRMED_FRAUD": 0, "FALSE_POSITIVE": 0, "INCONCLUSIVE": 0}
        for case in overdue:
            outcome = outcome_for(case)
            tally[outcome] += 1
            # Closed inside its target time: between 30% and 90% of the clock.
            took = target(case) * rng.uniform(0.3, 0.9)
            conn.execute(
                "UPDATE cases SET state='UNDER_REVIEW', assignee_id=%s WHERE id=%s",
                (analyst, case["id"]),
            )
            chain.append(conn, actor_user_id=analyst, action="CASE_REVIEW_STARTED",
                         object_type="case", object_id=case["id"],
                         from_state="OPEN", to_state="UNDER_REVIEW")
            conn.execute("UPDATE cases SET outcome=%s WHERE id=%s", (outcome, case["id"]))
            chain.append(conn, actor_user_id=analyst, action="CASE_OUTCOME_SET",
                         object_type="case", object_id=case["id"], to_state=outcome)
            conn.execute(
                "UPDATE cases SET state='CLOSED', "
                "closed_at=LEAST(opened_at + (interval '1 minute' * %s), now()), "
                "closed_by=%s WHERE id=%s",
                (took, lead, case["id"]),
            )
            chain.append(conn, actor_user_id=lead, action="CASE_CLOSED",
                         object_type="case", object_id=case["id"],
                         from_state="UNDER_REVIEW", to_state="CLOSED",
                         payload={"outcome": outcome})

        for case in in_review:
            conn.execute(
                "UPDATE cases SET state='UNDER_REVIEW', assignee_id=%s WHERE id=%s",
                (analyst, case["id"]),
            )
            chain.append(conn, actor_user_id=analyst, action="CASE_REVIEW_STARTED",
                         object_type="case", object_id=case["id"],
                         from_state="OPEN", to_state="UNDER_REVIEW")

        conn.commit()
        decided = sum(tally.values())
        print(f"    {decided} overdue cases worked and closed, {len(in_review)} in progress, "
              f"{len(waiting) - len(in_review)} waiting")
        print(f"    outcomes from truth: {tally['CONFIRMED_FRAUD']} fraud, "
              f"{tally['FALSE_POSITIVE']} false alarm, {tally['INCONCLUSIVE']} inconclusive")


def summarise() -> None:
    import psycopg

    from riskradar.config import settings

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        q = lambda s: conn.execute(s).fetchone()  # noqa: E731
        tx = q("SELECT count(*) n FROM transactions")["n"]
        dec = q("SELECT count(*) n FROM decisions")["n"]
        al = q("SELECT count(*) n FROM alerts")["n"]
        open_cases = q(
            "SELECT count(*) n FROM cases WHERE state IN ('OPEN','UNDER_REVIEW','ESCALATED')"
        )["n"]
        closed = q("SELECT count(*) n FROM cases WHERE state='CLOSED'")["n"]
        exposure = q(
            """
            SELECT COALESCE(sum(t.amount_minor), 0)::bigint n
              FROM alerts a JOIN transactions t ON t.id = a.transaction_id
              JOIN cases c ON c.id = a.case_id
             WHERE t.auth_result = 'APPROVED'
               AND c.state IN ('OPEN','UNDER_REVIEW','ESCALATED')
            """
        )["n"]
        th = q("SELECT version, p_monitor FROM threshold_sets WHERE is_active")

    print(f"    transactions      {tx:,}")
    print(f"    decisions         {dec:,}")
    print(f"    alerts            {al:,}  ({100 * al / max(tx, 1):.2f}% of traffic)")
    print(f"    OPEN CASES        {open_cases}     <- the worklist")
    print(f"    closed            {closed}")
    print(f"    exposure at risk  NGN {exposure / 100:,.0f}")
    print(f"    thresholds        v{th['version']}, p_monitor={float(th['p_monitor']):.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild a believable demo dataset")
    parser.add_argument("--stage", choices=["schema", "data"], required=True)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument(
        # 24, not 9. A window that ends at "now" and covers only nine hours is
        # overnight whenever the demo is rebuilt early in the morning — the
        # simulated customers are asleep, and the desk comes out almost empty
        # (observed: 1 open case). A full day always contains a waking day, and
        # at a 120/day budget it is exactly one day of the team's work.
        "--alerting-hours", type=float, default=24.0,
        help="how much recent traffic is allowed to raise alerts",
    )
    args = parser.parse_args()

    if args.stage == "schema":
        stage_schema()
    else:
        stage_data(args)


if __name__ == "__main__":
    main()

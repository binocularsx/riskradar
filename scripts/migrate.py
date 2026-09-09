"""Migration runner.

Numbered SQL files, applied in order, recorded in schema_migrations, run as the
migration role. Deliberately not Alembic: the schema has a hard requirement that
one role owns the objects and a different role gets narrow grants (D12c), and a
50-line runner that makes that obvious beats a framework that hides it.

    python scripts/migrate.py            # apply pending migrations
    python scripts/migrate.py --status   # show what is applied
    python scripts/migrate.py --reset    # drop and recreate the database (dev only)
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import psycopg  # noqa: E402

from riskradar.config import settings  # noqa: E402

MIGRATIONS_DIR = REPO_ROOT / "backend" / "migrations"
BOOTSTRAP = "0000_bootstrap.sql"

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    sha256     text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def _superuser_dsn(dbname: str = "postgres") -> str:
    s = settings()
    import os

    password = os.environ.get("RISKRADAR_PG_SUPERUSER_PASSWORD", "riskradar_dev_superuser_pw")
    user = os.environ.get("RISKRADAR_PG_SUPERUSER", "postgres")
    return f"host={s.db_host} port={s.db_port} dbname={dbname} user={user} password={password}"


def bootstrap() -> None:
    """Create the two roles and the database. Idempotent."""
    s = settings()
    sql = (MIGRATIONS_DIR / BOOTSTRAP).read_text(encoding="utf-8")

    with psycopg.connect(_superuser_dsn(), autocommit=True) as conn:
        conn.execute(sql)
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (s.db_name,)
        ).fetchone()
        if not exists:
            # Cannot be parameterised, and the name comes from our own config.
            conn.execute(f'CREATE DATABASE "{s.db_name}" OWNER riskradar_migrate')
            print(f"created database {s.db_name}")
        else:
            print(f"database {s.db_name} already exists")

    # The app role needs CONNECT; the migrate role owns the database.
    with psycopg.connect(_superuser_dsn(s.db_name), autocommit=True) as conn:
        conn.execute(f'GRANT CONNECT ON DATABASE "{s.db_name}" TO riskradar_app')
        # Stop the app role creating objects it would then own.
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("GRANT CREATE ON SCHEMA public TO riskradar_migrate")


def reset() -> None:
    s = settings()
    with psycopg.connect(_superuser_dsn(), autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (s.db_name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{s.db_name}"')
    print(f"dropped {s.db_name}")


def pending(conn: psycopg.Connection) -> list[Path]:
    conn.execute(TRACKING_TABLE)
    applied = {
        row[0]
        for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
    }
    files = sorted(
        p for p in MIGRATIONS_DIR.glob("*.sql") if p.name != BOOTSTRAP
    )
    return [p for p in files if p.name not in applied]


def apply() -> None:
    bootstrap()
    with psycopg.connect(settings().migrate_dsn) as conn:
        conn.autocommit = False
        todo = pending(conn)
        conn.commit()
        if not todo:
            print("no pending migrations")
            return
        for path in todo:
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            print(f"applying {path.name} ...", end=" ", flush=True)
            try:
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (filename, sha256) VALUES (%s, %s)",
                    (path.name, digest),
                )
                conn.commit()
                print("ok")
            except Exception:
                conn.rollback()
                print("FAILED")
                raise


def status() -> None:
    with psycopg.connect(settings().migrate_dsn) as conn:
        conn.execute(TRACKING_TABLE)
        rows = conn.execute(
            "SELECT filename, applied_at FROM schema_migrations ORDER BY filename"
        ).fetchall()
        for filename, applied_at in rows:
            print(f"  applied  {filename}  {applied_at:%Y-%m-%d %H:%M:%S}")
        todo = pending(conn)
        for path in todo:
            print(f"  PENDING  {path.name}")
        if not rows and not todo:
            print("  (nothing)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Risk Radar migration runner")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--reset", action="store_true", help="drop the database (dev only)")
    args = parser.parse_args()

    if args.reset:
        if settings().is_production:
            raise SystemExit("refusing to --reset in production")
        reset()
        return
    if args.status:
        status()
        return
    apply()


if __name__ == "__main__":
    main()

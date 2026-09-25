"""Save and restore the whole demo database. `python scripts/snapshot.py --save`

Why this exists
---------------
`demo_reset.py` builds a believable desk in the order a real deployment would —
load a month of history with alerting off, derive thresholds from that traffic,
then replay a window with alerting on. That ordering is the reason the data is
trustworthy, and it is also why it takes about three hours: the history is
~400,000 payments posted through the real API and scored by the real workers.

Doing that work *again* to get back to the same desk is waste. The output is a
database, and a database can be copied. Save one snapshot after a good rebuild
and any later "give me a working demo" is a restore — minutes, not hours.

    python scripts/snapshot.py --save                 # after a good rebuild
    python scripts/snapshot.py --restore              # back to that desk
    python scripts/snapshot.py --list

The snapshot lands in `fixtures/`, which .gitignore already excludes: it is
hundreds of megabytes and it is reproducible from the simulator with a fixed
seed, so it does not belong in version control.

A restore replaces the database wholesale, so **stop the services first**
(`python scripts/down.py`) — the drop cannot proceed while they hold
connections. It contains only synthetic simulator traffic (D28, D101a).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures"
DEFAULT_DUMP = FIXTURES / "demo.dump"
PG_BIN = Path(os.environ.get("RISKRADAR_PG_BIN", Path.home() / ".local" / "pgsql" / "bin"))

sys.path.insert(0, str(REPO_ROOT / "backend"))
from riskradar.config import settings  # noqa: E402


def _superuser_env() -> dict:
    env = dict(os.environ)
    env["PGPASSWORD"] = os.environ.get("RISKRADAR_PG_SUPERUSER_PASSWORD", "riskradar_dev_superuser_pw")
    return env


def _conn_args() -> list[str]:
    s = settings()
    user = os.environ.get("RISKRADAR_PG_SUPERUSER", "postgres")
    return ["-h", s.db_host, "-p", str(s.db_port), "-U", user]


def _run(tool: str, args: list[str]) -> None:
    exe = PG_BIN / f"{tool}.exe"
    if not exe.exists():
        raise SystemExit(f"{tool} not found at {exe} — set RISKRADAR_PG_BIN")
    result = subprocess.run([str(exe), *args], env=_superuser_env(),
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"{tool} failed")


def save(path: Path) -> None:
    s = settings()
    FIXTURES.mkdir(exist_ok=True)
    print(f"  dumping {s.db_name} -> {path}")
    started = time.time()
    # Custom format: compressed, and restorable in parallel.
    _run("pg_dump", [*_conn_args(), "-Fc", "-f", str(path), s.db_name])
    size_mb = path.stat().st_size / 1_000_000
    print(f"  saved {size_mb:,.0f} MB in {time.time() - started:.0f}s")
    print("\n  restore it any time with:  python scripts/snapshot.py --restore")


def restore(path: Path, jobs: int) -> None:
    if not path.exists():
        raise SystemExit(f"no snapshot at {path} — run --save after a good rebuild")
    s = settings()
    started = time.time()
    # Clear the services first. `dropdb --force` terminates the connections it
    # finds, but the API pool reconnects immediately, so the drop can lose that
    # race and block indefinitely against a database that looks idle.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import down

    print("  stopping services that hold connections")
    down.stop_matching(exclude_like="snapshot.py")
    print(f"  restoring {path.name} -> {s.db_name}")
    # Drop and recreate rather than restore over the top: a partial overlay
    # leaves rows from two different runs in the same tables, which is worse
    # than either and very hard to notice.
    _run("dropdb", [*_conn_args(), "--if-exists", "--force", s.db_name])
    _run("createdb", [*_conn_args(), "-O", "riskradar_migrate", s.db_name])
    # Ownership is preserved deliberately. --no-owner leaves every object owned
    # by the restoring superuser, and riskradar_migrate then cannot so much as
    # write schema_migrations — the next migration fails with a permission error
    # that looks nothing like its cause. The roles are cluster-level and survive
    # the drop, so the dump's ownership is restorable as-is.
    _run("pg_restore", [*_conn_args(), "-d", s.db_name, "-j", str(jobs), str(path)])
    print(f"  restored in {time.time() - started:.0f}s")
    print("\n  bring it up with:  python scripts/up.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Save or restore the demo database")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--file", type=Path, default=DEFAULT_DUMP)
    ap.add_argument("--jobs", type=int, default=4, help="parallel restore workers")
    args = ap.parse_args()

    print("\nRisk Radar - snapshot\n")
    if args.list or not (args.save or args.restore):
        if not FIXTURES.exists() or not list(FIXTURES.glob("*.dump")):
            print("  no snapshots yet. Make one with --save after a good rebuild.")
        for f in sorted(FIXTURES.glob("*.dump")):
            stat = f.stat()
            print(f"  {f.name:<28}{stat.st_size / 1_000_000:>8,.0f} MB   "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime))}")
    elif args.save:
        save(args.file)
    else:
        restore(args.file, args.jobs)
    print()

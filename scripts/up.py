"""Bring the whole demo up with one command. `python scripts/up.py`

Why this exists
---------------
Starting the system by hand is five terminals in a particular order — Postgres
on a non-default port, the API, the workers, the clock sweep, the live feed —
and getting the order or the port wrong fails in ways that look like something
else (a missing `-o "-p 55432"` starts Postgres on 5432, where every service
then times out against a server whose own log cheerfully says it is ready).

This does the order, checks each step actually came up, and prints where to go.
It is the everyday path: **it does not rebuild anything.** The database already
on disk is reused, so this is seconds-to-a-minute, not hours. Use
`scripts/snapshot.py --restore` for a clean desk, and `demo_reset.py` only when
you genuinely want new history generated from scratch.

    python scripts/up.py              # everything, including the live feed
    python scripts/up.py --no-feed    # leave the transaction feed off
    python scripts/up.py --rate 8     # busier feed
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
LOGS = REPO_ROOT / "logs"
PIDFILE = LOGS / "demo-pids.json"

# The portable Postgres lives outside the repo; both are overridable so this
# works on a machine that installed it somewhere else.
PG_BIN = Path(os.environ.get("RISKRADAR_PG_BIN", Path.home() / ".local" / "pgsql" / "bin"))
PGDATA = Path(os.environ.get("RISKRADAR_PGDATA", Path.home() / ".local" / "riskradar-pgdata"))
PGLOG = Path(os.environ.get("RISKRADAR_PG_LOG", Path.home() / ".local" / "riskradar-pg.log"))

sys.path.insert(0, str(REPO_ROOT / "backend"))
from riskradar.config import settings  # noqa: E402


def listening(port: int, host: str = "127.0.0.1") -> bool:
    # create_connection, not socket() — a bare socket is AF_INET, so probing an
    # IPv6 address with it always fails and reports a running server as down.
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


def vite_listening() -> bool:
    # Vite binds IPv6 by default here, so a v4-only probe reports it down.
    return listening(5173) or listening(5173, "::1")


def say(step: str, detail: str = "") -> None:
    print(f"  {step:<34}{detail}", flush=True)


def start_postgres(port: int) -> bool:
    if listening(port):
        say("postgres", f"already up on {port}")
        return True
    pg_ctl = PG_BIN / "pg_ctl.exe"
    if not pg_ctl.exists():
        raise SystemExit(f"pg_ctl not found at {pg_ctl} — set RISKRADAR_PG_BIN")
    # -o "-p <port>" is not optional: without it the server starts on 5432 and
    # every service times out against a database that is running perfectly.
    # Not capture_output: `pg_ctl start` hands its stdout to the postmaster it
    # spawns, and the postmaster keeps that handle open for as long as the
    # server runs — so a captured pipe never closes and subprocess.run waits
    # forever on a database that started perfectly.
    subprocess.run(
        [str(pg_ctl), "-D", str(PGDATA), "-l", str(PGLOG), "-o", f"-p {port}", "-w", "-t", "90", "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    for _ in range(90):
        if listening(port):
            say("postgres", f"started on {port}")
            return True
        time.sleep(1)
    raise SystemExit(f"postgres did not come up on {port}; see {PGLOG}")


def spawn(name: str, args: list[str], cwd: Path, env: dict | None = None) -> int:
    """Start a service detached, so it outlives this script and the shell."""
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / f"{name}.log", "ab")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        args, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=flags, close_fds=True, env=env,
    )
    return proc.pid


def api_healthy(timeout_s: int = 120) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=4) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1.5)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Start the Risk Radar demo")
    ap.add_argument("--no-feed", action="store_true", help="do not start the live transaction feed")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate", type=float, default=3.0, help="feed rate, payments a second")
    ap.add_argument("--share", action="store_true",
                    help="also publish it, so the demo can be reached from another machine")
    ap.add_argument("--vercel", action="store_true",
                    help="with --share, also repoint the shared Vercel site at this tunnel")
    args = ap.parse_args()

    s = settings()
    started = time.time()
    # D109: on the demo desk the simulator plays the support team, so messages
    # to support are accepted rather than left waiting for a real ticketing
    # system. A deployment that sets its own connector keeps it.
    os.environ.setdefault("RISKRADAR_SUPPORT_CONNECTOR", "loopback")
    pids: dict[str, int] = {}
    print("\nRisk Radar - starting\n")

    start_postgres(s.db_port)

    # Cheap and idempotent, and it catches the case where the code has moved on
    # from the database on disk — which otherwise surfaces as a confusing 500.
    migrated = subprocess.run([str(PYTHON), "scripts/migrate.py"], cwd=str(REPO_ROOT),
                              capture_output=True, text=True)
    applied = [ln for ln in migrated.stdout.splitlines() if "applying" in ln]
    say("migrations", f"{len(applied)} applied" if applied else "already current")

    if listening(8000):
        say("api", "already up on 8000")
    else:
        pids["api"] = spawn("api", [str(PYTHON), "-u", "scripts/run_api.py"], REPO_ROOT)
        say("api", "starting on 8000")

    health = api_healthy()
    if not health:
        raise SystemExit(f"the API did not become healthy — see {LOGS / 'api.log'}")
    say("api health", f"{health['status']}, model={'yes' if health['active_model'] else 'NO'}")

    pids["workers"] = spawn("workers", [str(PYTHON), "-u", "scripts/run_workers.py", str(args.workers)], REPO_ROOT)
    say("workers", f"{args.workers} scoring workers")
    pids["clocks"] = spawn("clocks", [str(PYTHON), "-u", "scripts/run_clocks.py", "60"], REPO_ROOT)
    say("clocks", "regulatory sweep every 60s")

    if not args.no_feed:
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "simulator"))
        pids["feed"] = spawn(
            "feed",
            [str(PYTHON), "-u", "-m", "riskradar_sim", "stream", "--rate", str(args.rate)],
            REPO_ROOT / "simulator", env,
        )
        say("live feed", f"{args.rate}/s - new alerts and cases arrive as it runs")
        pids["support"] = spawn(
            "support",
            [str(PYTHON), "-u", "-m", "riskradar_sim", "support", "work", "--every", "30"],
            REPO_ROOT / "simulator", env,
        )
        say("support team", "simulated - carries out approved actions every 30s")

    if vite_listening():
        say("frontend", "already up on 5173")
    else:
        pids["vite"] = spawn("vite", ["npm.cmd" if os.name == "nt" else "npm", "run", "dev"],
                             REPO_ROOT / "frontend")
        say("frontend", "starting on 5173")
        for _ in range(60):
            if vite_listening():
                break
            time.sleep(1)

    LOGS.mkdir(exist_ok=True)
    PIDFILE.write_text(json.dumps(pids), encoding="utf-8")

    print(f"\n  ready in {time.time() - started:.0f}s      http://localhost:5173\n")
    if args.share:
        # share.py refuses to start until the API answers, which it now does.
        # It prints its own progress and the public address, so let it write
        # to this console rather than being captured and reprinted.
        share_cmd = [str(PYTHON), "-u", "scripts/share.py"]
        if args.vercel:
            share_cmd.append("--vercel")
        subprocess.run(share_cmd, cwd=str(REPO_ROOT))

    print("  logs      scripts/../logs/*.log")
    print("  stop      python scripts/down.py        (closes the public address too)")
    print("  codes     python scripts/codes.py        (live MFA codes)\n")


if __name__ == "__main__":
    main()

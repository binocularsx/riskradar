"""Stop everything `up.py` started. `python scripts/down.py`

Matches on the repository's own virtualenv and frontend, so it never touches
another Python or Node on the machine. Postgres is left running by default —
it is cheap, and stopping it is the slow part of starting again.

    python scripts/down.py             # services, feed and frontend
    python scripts/down.py --postgres  # those, and the database server too
"""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_BIN = Path(os.environ.get("RISKRADAR_PG_BIN", Path.home() / ".local" / "pgsql" / "bin"))
PGDATA = Path(os.environ.get("RISKRADAR_PGDATA", Path.home() / ".local" / "riskradar-pgdata"))


def stop_matching(exclude_like: str | None = None) -> int:
    """Kill this repo's Python services and its Vite server, and nothing else.

    ``exclude_like`` spares a command line containing that text, so a caller
    running from the same virtualenv (snapshot.py, clearing connections before
    it drops the database) does not kill itself halfway through.
    """
    # No backslash-doubling: PowerShell's -like treats backslash as an ordinary
    # character, so an escaped path matches nothing and this silently kills
    # nothing while reporting success.
    venv = str(REPO_ROOT / ".venv")
    frontend = str(REPO_ROOT / "frontend")
    spare = f" -and $_.CommandLine -notlike '*{exclude_like}*'" if exclude_like else ""
    script = (
        "$killed = 0; "
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='node.exe'\" | "
        f"Where-Object {{ ($_.CommandLine -like '*{venv}*' -or "
        f"$_.CommandLine -like '*{frontend}*'){spare} }} | "
        "ForEach-Object { Write-Output ('  stopped ' + $_.ProcessId + '  ' + "
        "$_.CommandLine.Substring(0, [Math]::Min(70, $_.CommandLine.Length))); "
        "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed++ }; "
        "Write-Output ('  total ' + $killed)"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    print(result.stdout.rstrip())
    return result.returncode


def stop_postgres() -> None:
    pg_ctl = PG_BIN / "pg_ctl.exe"
    if not pg_ctl.exists():
        print(f"  pg_ctl not found at {pg_ctl}")
        return
    out = subprocess.run([str(pg_ctl), "-D", str(PGDATA), "-w", "-t", "60", "stop"],
                         capture_output=True, text=True)
    print("  " + (out.stdout.strip() or out.stderr.strip() or "postgres stopped"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Stop the Risk Radar demo")
    ap.add_argument("--postgres", action="store_true", help="stop the database server too")
    args = ap.parse_args()
    print("\nRisk Radar - stopping\n")
    stop_matching()
    if args.postgres:
        stop_postgres()
    print()

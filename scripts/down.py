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
import sys
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
    # Kill the whole tree (`taskkill /T`), not just the process that matched. With
    # more than one API worker uvicorn starts them through multiprocessing, and each
    # runs on the base interpreter, so its command line names neither the virtualenv
    # nor the frontend and the match never sees it. Stopping only the parent left the
    # workers alive, holding port 8000, and the next start could not bind it.
    script = (
        "$killed = 0; "
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='node.exe'\" | "
        f"Where-Object {{ ($_.CommandLine -like '*{venv}*' -or "
        f"$_.CommandLine -like '*{frontend}*'){spare} }} | "
        "ForEach-Object { Write-Output ('  stopped ' + $_.ProcessId + '  ' + "
        "$_.CommandLine.Substring(0, [Math]::Min(70, $_.CommandLine.Length))); "
        "& taskkill /PID $_.ProcessId /T /F *> $null; $killed++ }; "
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
    # The tunnel is cloudflared.exe, which is neither this repo's Python nor
    # its Node, so stop_matching() never sees it. Closing it here means one
    # stop command rather than two, and no tunnel left on after the API goes.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import share

    if share.STATE.exists():
        share.stop()
    # Spare this process: it is itself this repo's Python, so an unguarded
    # sweep kills the sweeper before it can report what it stopped.
    stop_matching(exclude_like="down.py")
    if args.postgres:
        stop_postgres()
    print()

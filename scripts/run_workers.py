"""Start N scoring workers. `python scripts/run_workers.py 3`

Competing consumers over one Postgres queue (D8a). Each worker leases a batch
with `UPDATE ... WHERE transaction_id IN (SELECT ... FOR UPDATE SKIP LOCKED)`
and then scores each transaction in its own short transaction, so no worker
holds a lock another one needs.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"

n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
procs = [
    subprocess.Popen([str(PYTHON), "-u", str(REPO_ROOT / "scripts" / "run_worker.py")])
    for _ in range(n)
]
print(f"started {n} worker(s): {[p.pid for p in procs]}")
try:
    for p in procs:
        p.wait()
except KeyboardInterrupt:
    for p in procs:
        p.terminate()

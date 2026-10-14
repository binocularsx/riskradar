"""Route unassigned cases to analysts (D94). `python scripts/route_cases.py`

The workers do this continuously for new cases. This is for the cases that were
already open when routing was switched on, and for an operator who wants the
unassigned exception queue cleared now rather than within the minute.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import psycopg  # noqa: E402

from riskradar.cases import assignment  # noqa: E402
from riskradar.config import settings  # noqa: E402
from riskradar.worker.scoring import system_user_id  # noqa: E402

if __name__ == "__main__":
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        done = assignment.sweep(conn, sys_uid=system_user_id(conn), limit=1000)
        conn.commit()
        for a in assignment.eligible(conn):
            print(f"  {a['display_name']:<24} {a['open_cases']} open")
    print(done)

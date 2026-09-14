"""Start the regulatory clock sweep. `python scripts/run_clocks.py [seconds]`

Records and escalates breached CBN clocks on reported cases (WP-05, D71), and
lifts expired 24-hour watch-list flags (WP-06, D73).
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from riskradar.clocks.sweep import run_forever

if __name__ == "__main__":
    run_forever(int(sys.argv[1]) if len(sys.argv) > 1 else 60)

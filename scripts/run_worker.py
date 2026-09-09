"""Start the scoring worker. `python scripts/run_worker.py`"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from riskradar.worker.scoring import main

if __name__ == "__main__":
    main()

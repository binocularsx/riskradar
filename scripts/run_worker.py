"""Start the scoring worker. `python scripts/run_worker.py`"""
import os
import sys
from pathlib import Path

# D87: one thread per worker for the model's arithmetic. scikit-learn's boosted
# trees predict with a thread per core by default; four workers on eight cores
# each spun up eight threads for every one-row prediction, and the contention
# more than doubled scoring time under load. Parallelism comes from running
# more workers, not from threads inside each. Set before numpy/sklearn load.
for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(var, "1")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from riskradar.worker.scoring import main  # noqa: E402

if __name__ == "__main__":
    main()

"""Start the API. `python scripts/run_api.py [--reload]`"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "riskradar.api.app:app",
        host="127.0.0.1",
        port=8000,
        reload="--reload" in sys.argv,
        log_level="info",
    )

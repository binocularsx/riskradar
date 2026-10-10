"""Start the API. `python scripts/run_api.py [--reload]`

Host, port and process count come from the environment (RISKRADAR_API_HOST,
RISKRADAR_API_PORT, RISKRADAR_API_WORKERS). Behind a TLS-terminating proxy in
production; the API itself speaks plain HTTP.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import uvicorn  # noqa: E402

from riskradar.config import settings  # noqa: E402
from riskradar.logsetup import configure  # noqa: E402

if __name__ == "__main__":
    s = settings()
    configure()
    reload = "--reload" in sys.argv
    uvicorn.run(
        "riskradar.api.app:app",
        host=s.api_host,
        port=s.api_port,
        reload=reload,
        workers=None if reload else s.api_workers,
        log_level="info",
        log_config=None,          # keep our handler; uvicorn's would replace it
        access_log=False,         # the request middleware writes one line per request
        proxy_headers=True,
        forwarded_allow_ips="*" if s.is_production else "127.0.0.1",
        timeout_graceful_shutdown=20,
    )

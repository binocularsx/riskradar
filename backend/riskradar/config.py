"""Runtime configuration.

Secrets come from the environment, never from source (NFR-005). The pepper is
the one that matters: it is one-way, so leaking it exposes nothing, but changing
it silently re-tokenises the world and orphans every behavioural baseline.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """Minimal .env loader.

    A dependency for this would be reasonable; a twelve-line function that never
    surprises anyone is more reasonable. Existing environment variables win, so
    containers and CI override the file rather than fight it.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


class Settings:
    """Resolved settings. Instantiated once via :func:`settings`."""

    def __init__(self) -> None:
        self.env = os.environ.get("RISKRADAR_ENV", "development")

        self.db_host = os.environ.get("RISKRADAR_DB_HOST", "127.0.0.1")
        self.db_port = int(os.environ.get("RISKRADAR_DB_PORT", "55432"))
        self.db_name = os.environ.get("RISKRADAR_DB_NAME", "riskradar")

        self.db_app_user = os.environ.get("RISKRADAR_DB_APP_USER", "riskradar_app")
        self.db_app_password = _require("RISKRADAR_DB_APP_PASSWORD")
        self.db_migrate_user = os.environ.get(
            "RISKRADAR_DB_MIGRATE_USER", "riskradar_migrate"
        )
        self.db_migrate_password = _require("RISKRADAR_DB_MIGRATE_PASSWORD")

        # D9c: HMAC-SHA256 pepper for one-way tokenisation at the ingestion boundary.
        self.hmac_pepper = _require("RISKRADAR_HMAC_PEPPER").encode("utf-8")

        self.session_cookie = os.environ.get("RISKRADAR_SESSION_COOKIE", "rr_session")
        # D27: 12h idle, 24h absolute.
        self.session_idle_hours = 12
        self.session_absolute_hours = 24

        self.artifact_dir = REPO_ROOT / "ml" / "artifacts"
        self.fixture_dir = REPO_ROOT / "fixtures"

        # --- serving (D87) -----------------------------------------------------
        self.db_pool_min = int(os.environ.get("RISKRADAR_DB_POOL_MIN", "2"))
        self.db_pool_max = int(os.environ.get("RISKRADAR_DB_POOL_MAX", "24"))
        # The console's origins. Development defaults to the Vite server; a
        # production deployment must name its own, or no browser can call it.
        default_origins = "" if self.env == "production" else "http://localhost:5173,http://127.0.0.1:5173"
        self.cors_origins = [o.strip() for o in os.environ.get("RISKRADAR_CORS_ORIGINS", default_origins).split(",")
                             if o.strip()]
        # Per API key, per API process: transactions a second, and the burst a
        # caller may send at once before the rate applies. A multi-instance
        # deployment divides the rate or enforces it at the gateway.
        self.ingest_rate = float(os.environ.get("RISKRADAR_INGEST_RATE", "300"))
        self.ingest_burst = float(os.environ.get("RISKRADAR_INGEST_BURST", "1500"))
        # Replayed history has its own, larger allowance: it is bounded by the
        # replay queue mark below, and a backfill should run at scoring speed.
        self.replay_rate = float(os.environ.get("RISKRADAR_REPLAY_RATE", "2000"))
        self.replay_burst = float(os.environ.get("RISKRADAR_REPLAY_BURST", "5000"))
        # Live payments waiting to be scored. Above the soft mark callers are
        # told to slow down; above the hard mark new work is refused with 503
        # and Retry-After, so a stalled worker fleet never turns into an
        # unbounded queue. Replayed history is refused earlier: it can wait.
        self.queue_soft_limit = int(os.environ.get("RISKRADAR_QUEUE_SOFT_LIMIT", "2000"))
        self.queue_hard_limit = int(os.environ.get("RISKRADAR_QUEUE_HARD_LIMIT", "20000"))
        self.replay_queue_limit = int(os.environ.get("RISKRADAR_REPLAY_QUEUE_LIMIT", "50000"))
        # Readiness: a worker unseen for this long is presumed dead.
        self.worker_stale_seconds = int(os.environ.get("RISKRADAR_WORKER_STALE_SECONDS", "60"))
        self.api_host = os.environ.get("RISKRADAR_API_HOST", "127.0.0.1")
        self.api_port = int(os.environ.get("RISKRADAR_API_PORT", "8000"))
        self.api_workers = int(os.environ.get("RISKRADAR_API_WORKERS", "1"))
        self.log_json = os.environ.get("RISKRADAR_LOG_JSON", "true" if self.env == "production" else "false") == "true"

    # -- connection strings -------------------------------------------------

    def _dsn(self, user: str, password: str) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={user} password={password}"
        )

    @property
    def app_dsn(self) -> str:
        """The application role. Cannot UPDATE or DELETE audit_log (D12c)."""
        return self._dsn(self.db_app_user, self.db_app_password)

    @property
    def migrate_dsn(self) -> str:
        """The migration role. Owns the schema; used only by scripts/migrate.py."""
        return self._dsn(self.db_migrate_user, self.db_migrate_password)

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()

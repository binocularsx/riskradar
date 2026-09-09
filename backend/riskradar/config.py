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

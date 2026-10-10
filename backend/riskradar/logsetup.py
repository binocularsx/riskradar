"""Logging for the API and the workers (D87).

One line per event. In production each line is a JSON object a log shipper can
index without a parsing rule; in development it is the readable text format the
scripts have always printed. Request logs carry the request id the caller saw
in ``X-Request-ID``, so a complaint about one response finds its log line.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .config import settings


class JsonFormatter(logging.Formatter):
    RESERVED = set(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in self.RESERVED and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    if settings().log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

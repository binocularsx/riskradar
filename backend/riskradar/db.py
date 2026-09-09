"""Database access.

One connection pool for the application role. Postgres is the queue (D8a), the
pub/sub doorbell (D14a) and the durable store, so this is the only I/O layer the
product has — which is the whole point of the decision: one durability story,
one backup strategy.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings().app_dsn,
            min_size=2,
            max_size=24,
            timeout=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextlib.contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection with an open transaction.

    Commits on clean exit, rolls back on exception. FR-005 depends on this:
    persisting a transaction and enqueuing it must be one database transaction,
    or a crash between them loses work silently.
    """
    with pool().connection() as conn:
        yield conn


def raw_connection(dsn: str | None = None) -> psycopg.Connection:
    """An unpooled connection.

    Used by the SSE listener, which holds a connection open on LISTEN for the
    lifetime of the stream and must not occupy a pool slot.
    """
    return psycopg.connect(dsn or settings().app_dsn, row_factory=dict_row, autocommit=True)


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(conn: psycopg.Connection, sql: str, params: Any = None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount

"""
Shared FastAPI dependencies: the connection pool, the transaction boundary,
and bearer-token auth.

Transaction rule (the reason this file exists): packages/db/transitions.py
locks a job row with SELECT ... FOR UPDATE and refuses to commit on the
caller's behalf. That lock is only held for the life of the transaction, so a
request handler must run inside exactly one transaction that commits when the
handler returns and rolls back if it raises. `txn()` is that boundary.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings

log = logging.getLogger("api.deps")


class Database:
    """Thin wrapper over a psycopg connection pool.

    Imported lazily so the module (and the unit tests) load without psycopg
    installed, matching the driver-free convention in packages/db.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8):
        self.dsn = dsn
        self._min, self._max = min_size, max_size
        self._pool: Any = None

    def open(self) -> None:
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            self.dsn or None, min_size=self._min, max_size=self._max, open=True
        )
        self._pool.wait(timeout=30)
        log.info("database pool open (min=%s max=%s)", self._min, self._max)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @property
    def is_open(self) -> bool:
        return self._pool is not None

    def connection(self):
        if self._pool is None:
            raise RuntimeError("database pool is not open")
        return self._pool.connection()

    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return True
        except Exception:  # noqa: BLE001 - health check reports, never raises
            log.warning("database ping failed", exc_info=True)
            return False


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_queue(request: Request):
    """The job queue the worker reserves from.

    The API pushes to it in exactly two places: when a job is created, and
    when a human approves the gate a job is parked on. Everything else the
    worker drives itself.
    """
    return request.app.state.queue


def txn(db: Database = Depends(get_db)) -> Iterator[Any]:
    """One transaction per request.

    psycopg's context manager commits on clean exit and rolls back on an
    exception, which is what keeps a FOR UPDATE lock correct across the
    handler and releases it either way.
    """
    with db.connection() as conn:
        yield conn


def require_token(
    settings: Settings = Depends(get_settings),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """
    Single shared bearer token (M1.3 scope — real users arrive in M6).

    Returns the caller identity to use as an audit actor. With no API_TOKEN
    configured, auth is disabled for local dev and the actor is "anonymous";
    /health reports that so an unprotected deploy is visible.
    """
    if not settings.auth_enabled:
        return "anonymous"

    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not credential:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    import hmac

    if not hmac.compare_digest(credential, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "api-token"

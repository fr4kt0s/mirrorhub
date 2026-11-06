"""SQLite engine (WAL), session helpers, migrations.

Transactions are explicit: the driver is put in autocommit mode and we emit BEGIN ourselves.
`immediate_session()` uses BEGIN IMMEDIATE, which the queue needs for atomic job claiming.
"""

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry
from sqlmodel import Session

from mirrorhub.settings import get_settings

ALEMBIC_INI = "/app/alembic.ini"


def _install_hooks(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: DBAPIConnection, _rec: ConnectionPoolEntry) -> None:
        dbapi_conn.isolation_level = None  # type: ignore[attr-defined]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        stmt = conn.get_execution_options().get("sqlite_begin", "BEGIN")
        conn.exec_driver_sql(stmt)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    settings.ensure_dirs()
    engine = create_engine(
        settings.db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )
    _install_hooks(engine)
    return engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Regular transaction (deferred BEGIN); commits on success, rolls back on error."""
    with Session(get_engine()) as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


@contextmanager
def immediate_session() -> Iterator[Session]:
    """Write-locking transaction (BEGIN IMMEDIATE) for race-free claim/lock operations."""
    engine = get_engine().execution_options(sqlite_begin="BEGIN IMMEDIATE")
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


def db_ok() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


def run_migrations() -> None:
    cfg = Config(ALEMBIC_INI)
    command.upgrade(cfg, "head")


def schema_ready() -> bool:
    try:
        with get_engine().connect() as conn:
            row: Any = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except Exception:
        return False
    return row is not None


def wait_for_schema(timeout_s: float = 120.0) -> None:
    """Workers call this at startup; only the web role runs migrations."""
    deadline = time.monotonic() + timeout_s
    while not schema_ready():
        if time.monotonic() > deadline:
            raise TimeoutError("database schema not ready")
        time.sleep(2)


if __name__ == "__main__":
    if sys.argv[1:] == ["migrate"]:
        run_migrations()
    else:
        print("usage: python -m mirrorhub.db migrate", file=sys.stderr)
        sys.exit(2)

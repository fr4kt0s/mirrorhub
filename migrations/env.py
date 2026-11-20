"""Alembic environment: online mode only, using the app's engine (WAL pragmas included)."""

from alembic import context
from sqlmodel import SQLModel

from mirrorhub import models  # noqa: F401  (registers tables)
from mirrorhub.db import get_engine

config = context.config
target_metadata = SQLModel.metadata


def run_migrations_online() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        # Batch migrations rebuild tables (copy, drop, rename). With foreign_keys=ON,
        # dropping a referenced table such as job fails, and the pragma is a no-op
        # inside a transaction. Switch it off on the raw driver connection before
        # SQLAlchemy begins, check integrity explicitly before commit, and restore it
        # so the pooled connection is returned in its normal state.
        raw = connection.connection.driver_connection
        assert raw is not None
        raw.execute("PRAGMA foreign_keys=OFF")
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                compare_type=True,
            )
            with context.begin_transaction():
                migration_context = context.get_context()
                before = migration_context.get_current_revision()
                context.run_migrations()
                if migration_context.get_current_revision() != before:
                    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise RuntimeError(
                            f"foreign key violations after migration: {violations[:10]}"
                        )
        finally:
            raw.execute("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported")
run_migrations_online()

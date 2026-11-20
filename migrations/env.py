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
        # SQLAlchemy begins, check integrity explicitly after the migrations, and
        # restore it so the pooled connection goes back to the pool in its normal
        # state.
        #
        # SQLite has no transactional DDL, so Alembic commits each migration itself
        # and begin_transaction() opens no outer transaction. Reads issued here
        # (current revision, foreign_key_check) autobegin one; it must be closed
        # before the pragma is restored, or "PRAGMA foreign_keys=ON" is ignored.
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
                applied = migration_context.get_current_revision() != before
            if connection.in_transaction():
                connection.commit()

            if applied:
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                connection.rollback()
                if violations:
                    raise RuntimeError(f"foreign key violations after migration: {violations[:10]}")
        finally:
            if connection.in_transaction():
                connection.rollback()
            raw.execute("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported")
run_migrations_online()

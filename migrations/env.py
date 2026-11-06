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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported")
run_migrations_online()

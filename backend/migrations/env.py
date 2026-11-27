"""Alembic environment (async engine; works with asyncpg and aiosqlite)."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from aerolink.service.db import metadata
from aerolink.service.settings import Settings

URL = context.config.get_main_option("sqlalchemy.url") or Settings.from_env().database_url


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = create_async_engine(URL)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=URL, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online())

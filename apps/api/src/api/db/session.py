from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api.config import Settings, get_settings

_engines: dict[str, AsyncEngine] = {}


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def get_async_engine(database_url: str) -> AsyncEngine:
    async_url = _async_database_url(database_url)
    engine = _engines.get(async_url)
    if engine is None:
        engine = create_async_engine(async_url, pool_pre_ping=True)
        _engines[async_url] = engine
    return engine


async def dispose_async_engines() -> None:
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()


def get_sessionmaker(settings: Settings) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_async_engine(settings.database_url),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_async_session(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker(settings)
    async with sessionmaker() as session:
        async with session.begin():
            yield session

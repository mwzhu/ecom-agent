from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings, get_settings
from api.db.session import get_async_session
from api.integrations.base import IntegrationRepository, SqlAlchemyIntegrationRepository


def get_integration_repository(
    session: Annotated[AsyncSession, Depends(get_async_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> IntegrationRepository:
    return SqlAlchemyIntegrationRepository(session, settings)

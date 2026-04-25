from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class MerchantTier(StrEnum):
    STARTER = "starter"
    GROWTH = "growth"
    SCALE = "scale"
    INTERNAL = "internal"


class ServiceName(StrEnum):
    API = "api"
    AGENTS = "agents"
    CONSOLE = "console"


class HealthResponse(BaseModel):
    service: ServiceName
    status: str = Field(pattern="^ok$")
    version: str


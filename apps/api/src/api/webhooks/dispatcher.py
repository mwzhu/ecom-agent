from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import Depends
from langgraph_sdk import get_client

from api.agents.dispatcher import langgraph_completion_webhook_url
from api.config import Settings, get_settings
from api.integrations import IntegrationProvider


@dataclass(frozen=True)
class WebhookDispatch:
    provider: IntegrationProvider
    event_id: str
    merchant_id: UUID
    case_id: UUID
    langgraph_thread_id: str
    exception_type: str
    order: dict[str, Any]
    context: dict[str, Any]
    payload: dict[str, Any]


class WebhookDispatcher(Protocol):
    async def create_thread(self) -> str:
        ...

    async def trigger(self, dispatch: WebhookDispatch) -> str | None:
        ...


class LangGraphWebhookDispatcher:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create_thread(self) -> str:
        client = get_client(url=self._settings.langgraph_studio_url)
        thread = await client.threads.create()
        thread_id = _value(thread, "thread_id") or _value(thread, "id")
        if thread_id is None:
            raise RuntimeError("LangGraph did not return a thread id.")
        return thread_id

    async def trigger(self, dispatch: WebhookDispatch) -> str | None:
        client = get_client(url=self._settings.langgraph_studio_url)
        run = await client.runs.create(
            dispatch.langgraph_thread_id,
            self._settings.langgraph_assistant_id,
            input={
                "merchant_id": str(dispatch.merchant_id),
                "case_id": str(dispatch.case_id),
                "exception_type": dispatch.exception_type,
                "order": dispatch.order,
                "context": dispatch.context,
                "webhook_payload": dispatch.payload,
            },
            metadata={
                "merchant_id": str(dispatch.merchant_id),
                "case_id": str(dispatch.case_id),
                "trigger": "webhook",
                "provider": dispatch.provider.value,
                "event_id": dispatch.event_id,
            },
            webhook=langgraph_completion_webhook_url(self._settings),
            multitask_strategy="reject",
        )
        return _value(run, "run_id") or _value(run, "id")


def get_webhook_dispatcher(
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookDispatcher:
    return LangGraphWebhookDispatcher(settings)


def _value(payload: Any, key: str) -> str | None:
    if isinstance(payload, dict):
        value = payload.get(key)
        return value if isinstance(value, str) else None
    value = getattr(payload, key, None)
    return value if isinstance(value, str) else None

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Protocol
from urllib.parse import urlencode

from fastapi import Depends
from langgraph_sdk import get_client

from api.config import Settings, get_settings

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True)
class ResumeResult:
    run_id: str | None
    submitted: bool


class CaseDecisionDispatcher(Protocol):
    async def resume_case(
        self,
        *,
        thread_id: str | None,
        merchant_id: UUID,
        case_id: UUID,
        decision: dict[str, Any],
    ) -> ResumeResult:
        ...


class LangGraphCaseDecisionDispatcher:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def resume_case(
        self,
        *,
        thread_id: str | None,
        merchant_id: UUID,
        case_id: UUID,
        decision: dict[str, Any],
    ) -> ResumeResult:
        if not thread_id:
            return ResumeResult(run_id=None, submitted=False)
        client = get_client(url=self._settings.langgraph_studio_url)
        run = await client.runs.create(
            thread_id,
            self._settings.langgraph_assistant_id,
            command={"resume": decision},
            metadata={
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "trigger": "human_decision",
                "decision": decision.get("decision"),
            },
            webhook=langgraph_completion_webhook_url(self._settings),
            multitask_strategy="reject",
        )
        return ResumeResult(run_id=_value(run, "run_id") or _value(run, "id"), submitted=True)


def get_case_decision_dispatcher(
    settings: Annotated[Settings, Depends(get_settings)],
) -> CaseDecisionDispatcher:
    return LangGraphCaseDecisionDispatcher(settings)


def _value(payload: Any, key: str) -> str | None:
    if isinstance(payload, dict):
        value = payload.get(key)
        return value if isinstance(value, str) else None
    value = getattr(payload, key, None)
    return value if isinstance(value, str) else None


def langgraph_completion_webhook_url(settings: Settings) -> str:
    base_url = settings.api_base_url.rstrip("/")
    query: dict[str, str] = {}
    if settings.langgraph_run_webhook_secret:
        query["token"] = settings.langgraph_run_webhook_secret
    suffix = f"?{urlencode(query)}" if query else ""
    return f"{base_url}/v1/agent-runs/langgraph-complete{suffix}"

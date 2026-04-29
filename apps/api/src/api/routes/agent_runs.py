from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from api.agents import LangGraphRunProcessor, get_langgraph_run_processor
from api.config import Settings, get_settings

router = APIRouter(prefix="/v1/agent-runs", tags=["agent-runs"])


class LangGraphCompletionResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str
    case_id: str
    case_status: str
    execution_status: str


@router.post("/langgraph-complete", response_model=LangGraphCompletionResponse)
async def langgraph_complete(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    processor: Annotated[LangGraphRunProcessor, Depends(get_langgraph_run_processor)],
) -> LangGraphCompletionResponse:
    _verify_langgraph_callback_token(request, settings)
    payload = await _parse_payload(request)
    run_id = _string(payload.get("run_id") or payload.get("id"))
    thread_id = _string(payload.get("thread_id"))
    if not run_id or not thread_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LangGraph callback payload must include run_id and thread_id.",
        )

    result = await processor.process_callback(thread_id=thread_id, run_id=run_id)
    return LangGraphCompletionResponse(
        run_id=result.run_id,
        thread_id=result.thread_id,
        status=result.status,
        case_id=str(result.case_id),
        case_status=result.case_status,
        execution_status=result.execution_status,
    )


def _verify_langgraph_callback_token(request: Request, settings: Settings) -> None:
    expected = settings.langgraph_run_webhook_secret
    if expected is None:
        return
    provided = request.query_params.get("token")
    if provided and hmac.compare_digest(provided, expected):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid LangGraph callback token.",
    )


async def _parse_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - malformed callback bodies should return 400.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LangGraph callback payload must be JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LangGraph callback payload must be a JSON object.",
        )
    return payload


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

from __future__ import annotations

from uuid import UUID

from langchain_core.tools import tool
from pydantic import Field

from api.integrations.base import (
    IntegrationProvider,
    JsonObject,
    JsonValue,
    ProviderCredential,
    ToolRequest,
    run_tool_with_session,
)
from api.integrations.http import ProviderHttpClient, ensure_object


class GmailGetThreadRequest(ToolRequest):
    thread_id: str = Field(min_length=1)
    format: str = Field(default="metadata", description="Gmail thread format.")


class GmailSearchThreadsRequest(ToolRequest):
    query: str = Field(min_length=1, description="Gmail search query.")
    max_results: int = Field(default=10, ge=1, le=50)


class GmailClient:
    def __init__(self, access_token: str) -> None:
        self._http = ProviderHttpClient(
            IntegrationProvider.GMAIL,
            base_url="https://gmail.googleapis.com/gmail/v1",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def get_thread(self, *, thread_id: str, format: str) -> JsonObject:
        return ensure_object(
            IntegrationProvider.GMAIL,
            await self._http.request_json(
                "GET",
                f"/users/me/threads/{thread_id}",
                params={"format": format},
            ),
        )

    async def search_threads(self, *, query: str, max_results: int) -> JsonObject:
        return ensure_object(
            IntegrationProvider.GMAIL,
            await self._http.request_json(
                "GET",
                "/users/me/threads",
                params={"q": query, "maxResults": max_results},
            ),
        )


@tool("gmail_get_thread", args_schema=GmailGetThreadRequest)
async def gmail_get_thread(
    merchant_id: UUID,
    case_id: UUID,
    thread_id: str,
    format: str = "metadata",
    idempotency_key: str | None = None,
) -> JsonObject:
    """Read a Gmail customer mail thread for address correction workflows."""

    request = GmailGetThreadRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        thread_id=thread_id,
        format=format,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await GmailClient(credential.access_token).get_thread(
            thread_id=thread_id,
            format=format,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.GMAIL,
        tool_name="gmail_get_thread",
        request=request,
        operation=operation,
    )


@tool("gmail_search_threads", args_schema=GmailSearchThreadsRequest)
async def gmail_search_threads(
    merchant_id: UUID,
    case_id: UUID,
    query: str,
    max_results: int = 10,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Search Gmail threads for customer/order correspondence."""

    request = GmailSearchThreadsRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        query=query,
        max_results=max_results,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await GmailClient(credential.access_token).search_threads(
            query=query,
            max_results=max_results,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.GMAIL,
        tool_name="gmail_search_threads",
        request=request,
        operation=operation,
    )

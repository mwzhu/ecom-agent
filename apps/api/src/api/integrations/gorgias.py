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
    WriteToolRequest,
    require_metadata_string,
    run_tool_with_session,
)
from api.integrations.http import ProviderHttpClient, ensure_object


class GorgiasGetTicketRequest(ToolRequest):
    account_domain: str | None = Field(default=None, description="Merchant Gorgias domain.")
    ticket_id: int = Field(ge=1)


class GorgiasSearchCustomerRequest(ToolRequest):
    account_domain: str | None = Field(default=None, description="Merchant Gorgias domain.")
    email: str = Field(min_length=3)


class GorgiasDraftReplyRequest(WriteToolRequest):
    account_domain: str | None = Field(default=None, description="Merchant Gorgias domain.")
    ticket_id: int = Field(ge=1)
    body_html: str = Field(min_length=1)
    channel: str = "email"
    from_agent: bool = False


class GorgiasClient:
    def __init__(self, access_token: str, account_domain: str) -> None:
        self._http = ProviderHttpClient(
            IntegrationProvider.GORGIAS,
            base_url=f"https://{_normalize_account_domain(account_domain)}/api",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def get_ticket(self, ticket_id: int) -> JsonObject:
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json("GET", f"/tickets/{ticket_id}"),
        )

    async def search_customer(self, email: str) -> JsonObject:
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json(
                "GET",
                "/customers",
                params={"email": email, "limit": 10},
            ),
        )

    async def draft_reply(
        self,
        *,
        ticket_id: int,
        body_html: str,
        channel: str,
        from_agent: bool,
    ) -> JsonObject:
        payload = {
            "body_html": body_html,
            "channel": channel,
            "via": "api",
            "from_agent": from_agent,
            "draft": True,
        }
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json(
                "POST",
                f"/tickets/{ticket_id}/messages",
                json_body=payload,
            ),
        )


@tool("gorgias_get_ticket", args_schema=GorgiasGetTicketRequest)
async def gorgias_get_ticket(
    merchant_id: UUID,
    case_id: UUID,
    ticket_id: int,
    account_domain: str | None = None,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Fetch a Gorgias ticket and message context."""

    request = GorgiasGetTicketRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        account_domain=account_domain,
        ticket_id=ticket_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await GorgiasClient(
            credential.access_token,
            _account_domain(request.account_domain, credential),
        ).get_ticket(ticket_id)

    return await run_tool_with_session(
        provider=IntegrationProvider.GORGIAS,
        tool_name="gorgias_get_ticket",
        request=request,
        operation=operation,
    )


@tool("gorgias_search_customer", args_schema=GorgiasSearchCustomerRequest)
async def gorgias_search_customer(
    merchant_id: UUID,
    case_id: UUID,
    email: str,
    account_domain: str | None = None,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Search Gorgias customer context by email."""

    request = GorgiasSearchCustomerRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        account_domain=account_domain,
        email=email,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await GorgiasClient(
            credential.access_token,
            _account_domain(request.account_domain, credential),
        ).search_customer(email)

    return await run_tool_with_session(
        provider=IntegrationProvider.GORGIAS,
        tool_name="gorgias_search_customer",
        request=request,
        operation=operation,
    )


@tool("gorgias_draft_reply", args_schema=GorgiasDraftReplyRequest)
async def gorgias_draft_reply(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    ticket_id: int,
    body_html: str,
    account_domain: str | None = None,
    channel: str = "email",
    from_agent: bool = False,
) -> JsonObject:
    """Create a draft Gorgias reply for human review."""

    request = GorgiasDraftReplyRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        account_domain=account_domain,
        ticket_id=ticket_id,
        body_html=body_html,
        channel=channel,
        from_agent=from_agent,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await GorgiasClient(
            credential.access_token,
            _account_domain(request.account_domain, credential),
        ).draft_reply(
            ticket_id=ticket_id,
            body_html=body_html,
            channel=channel,
            from_agent=from_agent,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.GORGIAS,
        tool_name="gorgias_draft_reply",
        request=request,
        operation=operation,
        write=True,
    )


def _account_domain(account_domain: str | None, credential: ProviderCredential) -> str:
    if account_domain:
        return account_domain
    fallback = credential.metadata.get("gorgias_domain")
    return require_metadata_string(
        credential,
        "account_domain",
        fallback if isinstance(fallback, str) and fallback else None,
    )


def _normalize_account_domain(account_domain: str) -> str:
    return account_domain.removeprefix("https://").removeprefix("http://").strip("/")

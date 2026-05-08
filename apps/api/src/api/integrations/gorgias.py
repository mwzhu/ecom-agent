from __future__ import annotations

import base64
from collections.abc import Mapping
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
    def __init__(
        self,
        access_token: str,
        account_domain: str,
        *,
        username: str | None = None,
        auth_scheme: str | None = None,
    ) -> None:
        self._username = username
        self._account_domain = _normalize_account_domain(account_domain)
        self._http = ProviderHttpClient(
            IntegrationProvider.GORGIAS,
            base_url=f"https://{self._account_domain}/api",
            headers=_auth_headers(
                access_token,
                username=username,
                auth_scheme=auth_scheme,
            ),
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

    async def list_tickets(self, *, limit: int = 50) -> JsonObject:
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json(
                "GET",
                "/tickets",
                params={"limit": max(1, min(limit, 100))},
            ),
        )

    async def list_integrations(self, *, limit: int = 100) -> JsonObject:
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json(
                "GET",
                "/integrations",
                params={"limit": max(1, min(limit, 100)), "type": "http"},
            ),
        )

    async def find_ticket_for_order(
        self,
        *,
        customer_email: str | None,
        order_name: str | None,
        limit: int = 100,
    ) -> JsonObject | None:
        tickets = await self.list_tickets(limit=limit)
        rows = tickets.get("data")
        if not isinstance(rows, list):
            return None
        normalized_email = (customer_email or "").strip().lower()
        normalized_order = (order_name or "").strip().lower()
        for item in rows:
            if not isinstance(item, dict):
                continue
            customer = item.get("customer")
            customer_email_value = customer.get("email") if isinstance(customer, dict) else None
            ticket_email = (
                customer_email_value if isinstance(customer_email_value, str) else ""
            ).strip().lower()
            subject = str(item.get("subject") or "").lower()
            excerpt = str(item.get("excerpt") or "").lower()
            external_id = str(item.get("external_id") or "").lower()
            email_matches = not normalized_email or ticket_email == normalized_email
            order_matches = not normalized_order or (
                normalized_order in subject
                or normalized_order in excerpt
                or normalized_order in external_id
            )
            if email_matches and order_matches:
                return item
        return None

    async def create_ticket(
        self,
        *,
        subject: str,
        customer_email: str,
        body_text: str,
        external_id: str,
        tags: list[str] | None = None,
    ) -> JsonObject:
        payload: JsonObject = {
            "customer": {"email": customer_email},
            "messages": [
                {
                    "sender": {"email": customer_email},
                    "source": {
                        "to": [{"address": self._username or "support@example.com"}],
                        "from": {"address": customer_email},
                    },
                    "body_html": body_text,
                    "body_text": body_text,
                    "channel": "email",
                    "from_agent": False,
                    "via": "api",
                }
            ],
            "meta": {"external_id": external_id, "generated_by": "flowlabs_real_demo"},
            "channel": "email",
            "external_id": external_id,
            "from_agent": False,
            "status": "open",
            "subject": subject,
            "via": "api",
        }
        if tags:
            payload["tags"] = [{"name": tag} for tag in tags]
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json("POST", "/tickets", json_body=payload),
        )

    async def draft_reply(
        self,
        *,
        ticket_id: int,
        body_html: str,
        channel: str,
        from_agent: bool,
    ) -> JsonObject:
        ticket = await self.get_ticket(ticket_id)
        customer = ticket.get("customer")
        customer_email = (
            customer.get("email")
            if isinstance(customer, dict) and isinstance(customer.get("email"), str)
            else None
        )
        source = _reply_source(
            ticket=ticket,
            channel=channel,
            from_address=self._username,
            customer_email=customer_email,
        )
        payload = {
            "body_text": body_html,
            "body_html": body_html,
            "channel": channel,
            "via": "api",
            "from_agent": from_agent,
            "draft": True,
        }
        if source:
            payload["source"] = source
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json(
                "POST",
                f"/tickets/{ticket_id}/messages",
                json_body=payload,
            ),
        )

    async def create_http_integration(
        self,
        *,
        topic: str,
        callback_url: str,
        webhook_secret: str | None,
    ) -> JsonObject:
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json(
                "POST",
                "/integrations",
                json_body=self._http_integration_payload(
                    topic=topic,
                    callback_url=callback_url,
                    webhook_secret=webhook_secret,
                ),
            ),
        )

    async def update_http_integration(
        self,
        *,
        integration_id: str,
        topic: str,
        callback_url: str,
        webhook_secret: str | None,
    ) -> JsonObject:
        return ensure_object(
            IntegrationProvider.GORGIAS,
            await self._http.request_json(
                "PUT",
                f"/integrations/{integration_id}",
                json_body=self._http_integration_payload(
                    topic=topic,
                    callback_url=callback_url,
                    webhook_secret=webhook_secret,
                ),
            ),
        )

    async def delete_integration(self, integration_id: str) -> None:
        await self._http.request_json("DELETE", f"/integrations/{integration_id}")

    def _http_integration_payload(
        self,
        *,
        topic: str,
        callback_url: str,
        webhook_secret: str | None,
    ) -> JsonObject:
        headers = {"X-Ecom-Webhook-Secret": webhook_secret} if webhook_secret else {}
        return {
            "name": f"Ecom Agent {topic}",
            "description": "Sends Gorgias ticket events to Ecom Agent.",
            "type": "http",
            "http": {
                "url": callback_url,
                "method": "POST",
                "headers": headers,
                "triggers": {_gorgias_trigger_key(topic): True},
                "form": {
                    "gorgias_domain": self._account_domain,
                    "webhook_secret": webhook_secret,
                    "topic": topic,
                    "ticket": {
                        "id": "{{ticket.id}}",
                        "subject": "{{ticket.subject}}",
                        "status": "{{ticket.status}}",
                    },
                    "customer": {
                        "email": "{{ticket.customer.email}}",
                        "name": "{{ticket.customer.name}}",
                    },
                    "message": {
                        "body_text": "{{ticket.last_message.body_text}}",
                        "body_html": "{{ticket.last_message.body_html}}",
                    },
                },
                "request_content_type": "application/json",
                "response_content_type": "application/json",
            },
        }


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
            username=_metadata_string(credential, "username"),
            auth_scheme=_metadata_string(credential, "auth_scheme"),
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
            username=_metadata_string(credential, "username"),
            auth_scheme=_metadata_string(credential, "auth_scheme"),
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
            username=_metadata_string(credential, "username"),
            auth_scheme=_metadata_string(credential, "auth_scheme"),
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


def _gorgias_trigger_key(topic: str) -> str:
    return topic.replace("-", "_")


def _reply_source(
    *,
    ticket: JsonObject,
    channel: str,
    from_address: str | None,
    customer_email: str | None,
) -> JsonObject | None:
    to_address = customer_email or _ticket_customer_email(ticket)
    sender_address = (
        from_address
        or _last_inbound_recipient_address(ticket)
        or _ticket_receiver_address(ticket)
    )
    if not sender_address or not to_address:
        return None
    return {
        "type": channel,
        "from": {"name": "Support", "address": sender_address},
        "to": [{"address": to_address}],
    }


def _ticket_customer_email(ticket: JsonObject) -> str | None:
    customer = ticket.get("customer") or ticket.get("requester")
    email = customer.get("email") if isinstance(customer, dict) else None
    if isinstance(email, str):
        return email
    return None


def _last_inbound_recipient_address(ticket: JsonObject) -> str | None:
    messages = ticket.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("from_agent") is True:
            continue
        source = message.get("source")
        if not isinstance(source, dict):
            continue
        recipients = source.get("to")
        if not isinstance(recipients, list):
            continue
        for recipient in recipients:
            address = recipient.get("address") if isinstance(recipient, dict) else None
            if isinstance(address, str):
                return address
    return None


def _ticket_receiver_address(ticket: JsonObject) -> str | None:
    messages = ticket.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        receiver = message.get("receiver")
        email = receiver.get("email") if isinstance(receiver, dict) else None
        if isinstance(email, str):
            return email
    return None


def _auth_headers(
    access_token: str,
    *,
    username: str | None,
    auth_scheme: str | None,
) -> Mapping[str, str]:
    if username and auth_scheme == "basic":
        token = base64.b64encode(f"{username}:{access_token}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": f"Bearer {access_token}"}


def _metadata_string(credential: ProviderCredential, key: str) -> str | None:
    value = credential.metadata.get(key)
    return value if isinstance(value, str) and value else None

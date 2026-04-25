from __future__ import annotations

from uuid import UUID

from langchain_core.tools import tool
from pydantic import Field

from api.integrations.base import (
    IntegrationError,
    IntegrationErrorKind,
    IntegrationProvider,
    JsonObject,
    JsonValue,
    ProviderCredential,
    ToolRequest,
    WriteToolRequest,
    run_tool_with_session,
)
from api.integrations.http import ProviderHttpClient, ensure_object


class StripeGetChargeRequest(ToolRequest):
    charge_id: str = Field(description="Stripe charge id, for example ch_123.")


class StripeGetDisputeRequest(ToolRequest):
    dispute_id: str = Field(description="Stripe dispute id, for example dp_123.")


class StripeListDisputesRequest(ToolRequest):
    charge_id: str | None = None
    payment_intent_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class StripeCreateRefundRequest(WriteToolRequest):
    charge_id: str = Field(description="Stripe charge id to refund.")
    amount: int | None = Field(
        default=None,
        description="Amount in the charge currency's minor unit.",
    )
    reason: str | None = Field(default=None)
    metadata: JsonObject = Field(default_factory=dict)
    approved_by: str = Field(min_length=1, description="Human approver for Phase 0 write gating.")


class StripeClient:
    def __init__(self, access_token: str) -> None:
        self._http = ProviderHttpClient(
            IntegrationProvider.STRIPE,
            base_url="https://api.stripe.com/v1",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def get_charge(self, charge_id: str) -> JsonObject:
        return ensure_object(
            IntegrationProvider.STRIPE,
            await self._http.request_json("GET", f"/charges/{charge_id}"),
        )

    async def get_dispute(self, dispute_id: str) -> JsonObject:
        return ensure_object(
            IntegrationProvider.STRIPE,
            await self._http.request_json("GET", f"/disputes/{dispute_id}"),
        )

    async def list_disputes(
        self,
        *,
        charge_id: str | None,
        payment_intent_id: str | None,
        limit: int,
    ) -> JsonObject:
        params: dict[str, str | int] = {"limit": limit}
        if charge_id is not None:
            params["charge"] = charge_id
        if payment_intent_id is not None:
            params["payment_intent"] = payment_intent_id
        return ensure_object(
            IntegrationProvider.STRIPE,
            await self._http.request_json("GET", "/disputes", params=params),
        )

    async def create_refund(
        self,
        *,
        charge_id: str,
        amount: int | None,
        reason: str | None,
        metadata: JsonObject,
        idempotency_key: str,
    ) -> JsonObject:
        form: dict[str, str] = {"charge": charge_id}
        if amount is not None:
            form["amount"] = str(amount)
        if reason is not None:
            form["reason"] = reason
        for key, value in metadata.items():
            form[f"metadata[{key}]"] = str(value)
        return ensure_object(
            IntegrationProvider.STRIPE,
            await self._http.request_json(
                "POST",
                "/refunds",
                headers={"Idempotency-Key": idempotency_key},
                data=form,
            ),
        )


@tool("stripe_get_charge", args_schema=StripeGetChargeRequest)
async def stripe_get_charge(
    merchant_id: UUID,
    case_id: UUID,
    charge_id: str,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Fetch a Stripe charge for payment exception context."""

    request = StripeGetChargeRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        charge_id=charge_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await StripeClient(credential.access_token).get_charge(charge_id)

    return await run_tool_with_session(
        provider=IntegrationProvider.STRIPE,
        tool_name="stripe_get_charge",
        request=request,
        operation=operation,
    )


@tool("stripe_get_dispute", args_schema=StripeGetDisputeRequest)
async def stripe_get_dispute(
    merchant_id: UUID,
    case_id: UUID,
    dispute_id: str,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Fetch a Stripe dispute for chargeback workup."""

    request = StripeGetDisputeRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        dispute_id=dispute_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await StripeClient(credential.access_token).get_dispute(dispute_id)

    return await run_tool_with_session(
        provider=IntegrationProvider.STRIPE,
        tool_name="stripe_get_dispute",
        request=request,
        operation=operation,
    )


@tool("stripe_list_disputes", args_schema=StripeListDisputesRequest)
async def stripe_list_disputes(
    merchant_id: UUID,
    case_id: UUID,
    charge_id: str | None = None,
    payment_intent_id: str | None = None,
    limit: int = 10,
    idempotency_key: str | None = None,
) -> JsonObject:
    """List Stripe disputes filtered by charge or payment intent."""

    request = StripeListDisputesRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        charge_id=charge_id,
        payment_intent_id=payment_intent_id,
        limit=limit,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await StripeClient(credential.access_token).list_disputes(
            charge_id=charge_id,
            payment_intent_id=payment_intent_id,
            limit=limit,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.STRIPE,
        tool_name="stripe_list_disputes",
        request=request,
        operation=operation,
    )


@tool("stripe_create_refund", args_schema=StripeCreateRefundRequest)
async def stripe_create_refund(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    charge_id: str,
    approved_by: str,
    amount: int | None = None,
    reason: str | None = None,
    metadata: JsonObject | None = None,
) -> JsonObject:
    """Issue a human-gated Stripe refund with provider idempotency."""

    request = StripeCreateRefundRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        charge_id=charge_id,
        amount=amount,
        reason=reason,
        metadata=metadata or {},
        approved_by=approved_by,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        if not approved_by:
            raise IntegrationError(
                IntegrationErrorKind.FATAL,
                IntegrationProvider.STRIPE,
                "Stripe refunds are write-gated in Phase 0 and require approved_by.",
            )
        return await StripeClient(credential.access_token).create_refund(
            charge_id=charge_id,
            amount=amount,
            reason=reason,
            metadata=request.metadata,
            idempotency_key=idempotency_key,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.STRIPE,
        tool_name="stripe_create_refund",
        request=request,
        operation=operation,
        write=True,
    )

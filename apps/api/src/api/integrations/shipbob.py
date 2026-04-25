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
    run_tool_with_session,
)
from api.integrations.http import ProviderHttpClient, ensure_object


class ShipBobGetOrderRequest(ToolRequest):
    order_id: int = Field(ge=1, description="ShipBob order id.")


class ShipBobGetShipmentRequest(ToolRequest):
    shipment_id: int = Field(ge=1, description="ShipBob shipment id.")


class ShipBobHoldOrderRequest(WriteToolRequest):
    order_id: int = Field(ge=1, description="ShipBob order id.")
    reason: str = Field(min_length=1)


class ShipBobClient:
    def __init__(self, access_token: str) -> None:
        self._http = ProviderHttpClient(
            IntegrationProvider.SHIPBOB,
            base_url="https://api.shipbob.com/1.0",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def get_order(self, order_id: int) -> JsonObject:
        return ensure_object(
            IntegrationProvider.SHIPBOB,
            await self._http.request_json("GET", f"/order/{order_id}"),
        )

    async def get_shipment(self, shipment_id: int) -> JsonObject:
        return ensure_object(
            IntegrationProvider.SHIPBOB,
            await self._http.request_json("GET", f"/shipment/{shipment_id}"),
        )

    async def hold_order(self, *, order_id: int, reason: str) -> JsonObject:
        return ensure_object(
            IntegrationProvider.SHIPBOB,
            await self._http.request_json(
                "POST",
                f"/order/{order_id}/hold",
                json_body={"reason": reason},
            ),
        )


@tool("shipbob_get_order", args_schema=ShipBobGetOrderRequest)
async def shipbob_get_order(
    merchant_id: UUID,
    case_id: UUID,
    order_id: int,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Fetch ShipBob order status and fulfillment metadata."""

    request = ShipBobGetOrderRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        order_id=order_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await ShipBobClient(credential.access_token).get_order(order_id)

    return await run_tool_with_session(
        provider=IntegrationProvider.SHIPBOB,
        tool_name="shipbob_get_order",
        request=request,
        operation=operation,
    )


@tool("shipbob_get_shipment", args_schema=ShipBobGetShipmentRequest)
async def shipbob_get_shipment(
    merchant_id: UUID,
    case_id: UUID,
    shipment_id: int,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Fetch ShipBob shipment tracking status."""

    request = ShipBobGetShipmentRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shipment_id=shipment_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await ShipBobClient(credential.access_token).get_shipment(shipment_id)

    return await run_tool_with_session(
        provider=IntegrationProvider.SHIPBOB,
        tool_name="shipbob_get_shipment",
        request=request,
        operation=operation,
    )


@tool("shipbob_hold_order", args_schema=ShipBobHoldOrderRequest)
async def shipbob_hold_order(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    order_id: int,
    reason: str,
) -> JsonObject:
    """Place a ShipBob order on fulfillment hold."""

    request = ShipBobHoldOrderRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        order_id=order_id,
        reason=reason,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await ShipBobClient(credential.access_token).hold_order(
            order_id=order_id,
            reason=reason,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.SHIPBOB,
        tool_name="shipbob_hold_order",
        request=request,
        operation=operation,
        write=True,
    )

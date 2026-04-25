from __future__ import annotations

import base64
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


class ShipStationGetOrderRequest(ToolRequest):
    order_id: int = Field(ge=1, description="ShipStation order id.")


class ShipStationGetShipmentRequest(ToolRequest):
    shipment_id: int = Field(ge=1, description="ShipStation shipment id.")


class ShipStationHoldOrderRequest(WriteToolRequest):
    order_id: int = Field(ge=1, description="ShipStation order id.")
    hold_until_date: str = Field(description="ISO-8601 date the order should remain on hold until.")


class ShipStationClient:
    def __init__(self, credential: ProviderCredential) -> None:
        self._http = ProviderHttpClient(
            IntegrationProvider.SHIPSTATION,
            base_url="https://ssapi.shipstation.com",
            headers={"Authorization": _authorization_header(credential)},
        )

    async def get_order(self, order_id: int) -> JsonObject:
        return ensure_object(
            IntegrationProvider.SHIPSTATION,
            await self._http.request_json("GET", f"/orders/{order_id}"),
        )

    async def get_shipment(self, shipment_id: int) -> JsonObject:
        return ensure_object(
            IntegrationProvider.SHIPSTATION,
            await self._http.request_json("GET", f"/shipments/{shipment_id}"),
        )

    async def hold_order(self, *, order_id: int, hold_until_date: str) -> JsonObject:
        return ensure_object(
            IntegrationProvider.SHIPSTATION,
            await self._http.request_json(
                "POST",
                "/orders/holduntil",
                json_body={"orderId": order_id, "holdUntilDate": hold_until_date},
            ),
        )


@tool("shipstation_get_order", args_schema=ShipStationGetOrderRequest)
async def shipstation_get_order(
    merchant_id: UUID,
    case_id: UUID,
    order_id: int,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Fetch ShipStation order status and warehouse details."""

    request = ShipStationGetOrderRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        order_id=order_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await ShipStationClient(credential).get_order(order_id)

    return await run_tool_with_session(
        provider=IntegrationProvider.SHIPSTATION,
        tool_name="shipstation_get_order",
        request=request,
        operation=operation,
    )


@tool("shipstation_get_shipment", args_schema=ShipStationGetShipmentRequest)
async def shipstation_get_shipment(
    merchant_id: UUID,
    case_id: UUID,
    shipment_id: int,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Fetch ShipStation shipment tracking context."""

    request = ShipStationGetShipmentRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shipment_id=shipment_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await ShipStationClient(credential).get_shipment(shipment_id)

    return await run_tool_with_session(
        provider=IntegrationProvider.SHIPSTATION,
        tool_name="shipstation_get_shipment",
        request=request,
        operation=operation,
    )


@tool("shipstation_hold_order", args_schema=ShipStationHoldOrderRequest)
async def shipstation_hold_order(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    order_id: int,
    hold_until_date: str,
) -> JsonObject:
    """Put a ShipStation order on hold until a specific date."""

    request = ShipStationHoldOrderRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        order_id=order_id,
        hold_until_date=hold_until_date,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        return await ShipStationClient(credential).hold_order(
            order_id=order_id,
            hold_until_date=hold_until_date,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.SHIPSTATION,
        tool_name="shipstation_hold_order",
        request=request,
        operation=operation,
        write=True,
    )


def _authorization_header(credential: ProviderCredential) -> str:
    api_secret = credential.metadata.get("api_secret")
    if isinstance(api_secret, str) and api_secret:
        token = base64.b64encode(f"{credential.access_token}:{api_secret}".encode())
        return f"Basic {token.decode('utf-8')}"
    if credential.access_token:
        return f"Bearer {credential.access_token}"
    raise IntegrationError(
        IntegrationErrorKind.AUTH_EXPIRED,
        IntegrationProvider.SHIPSTATION,
        "ShipStation credential is missing an API key.",
    )

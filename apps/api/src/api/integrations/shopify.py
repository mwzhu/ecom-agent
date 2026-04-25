from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.tools import tool
from pydantic import Field

from api.config import get_settings
from api.integrations.base import (
    IntegrationError,
    IntegrationErrorKind,
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


class ShopifyGetOrderRequest(ToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    order_id: str = Field(description="Shopify GraphQL Order gid.")


class ShopifyUpdateOrderNoteRequest(WriteToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    order_id: str = Field(description="Shopify GraphQL Order gid.")
    note: str = Field(min_length=1, description="Replacement note to store on the order.")


class ShopifyCreateRefundRequest(WriteToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    order_id: str = Field(description="Shopify GraphQL Order gid.")
    note: str | None = None
    notify_customer: bool = True
    refund_line_items: list[JsonObject] = Field(default_factory=list)
    transactions: list[JsonObject] = Field(default_factory=list)
    shipping: JsonObject | None = None


class ShopifyCancelOrderRequest(WriteToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    order_id: str = Field(description="Shopify GraphQL Order gid.")
    reason: str = Field(default="OTHER")
    refund: bool = False
    restock: bool = True
    notify_customer: bool = True
    staff_note: str | None = None


class ShopifyHoldFulfillmentOrderRequest(WriteToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    fulfillment_order_id: str = Field(description="Shopify GraphQL FulfillmentOrder gid.")
    reason: str = Field(default="OTHER")
    reason_notes: str = Field(min_length=1)


class ShopifyClient:
    def __init__(self, access_token: str, shop_domain: str) -> None:
        settings = get_settings()
        safe_shop_domain = _normalize_shop_domain(shop_domain)
        self._http = ProviderHttpClient(
            IntegrationProvider.SHOPIFY,
            base_url=(
                f"https://{safe_shop_domain}/admin/api/"
                f"{settings.shopify_admin_api_version}/graphql.json"
            ),
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
        )

    async def graphql(self, query: str, variables: JsonObject | None = None) -> JsonObject:
        response = ensure_object(
            IntegrationProvider.SHOPIFY,
            await self._http.request_json(
                "POST",
                "",
                json_body={"query": query, "variables": variables or {}},
            ),
        )
        errors = response.get("errors")
        if errors:
            raise IntegrationError(
                IntegrationErrorKind.FATAL,
                IntegrationProvider.SHOPIFY,
                "Shopify GraphQL returned errors.",
                details={"errors": errors},
            )
        return response

    async def get_order(self, order_id: str) -> JsonObject:
        return await self.graphql(
            """
            query EcomAgentOrder($id: ID!) {
              order(id: $id) {
                id
                name
                displayFinancialStatus
                displayFulfillmentStatus
                totalPriceSet { shopMoney { amount currencyCode } }
                customer { id email displayName numberOfOrders }
                shippingAddress {
                  name address1 address2 city province country zip phone
                }
                risk { assessments { riskLevel facts { description sentiment } } }
                fulfillmentOrders(first: 20) {
                  nodes {
                    id status requestStatus supportedActions { action }
                    assignedLocation { name location { id name } }
                  }
                }
                lineItems(first: 50) {
                  nodes { id sku title quantity refundableQuantity fulfillableQuantity }
                }
              }
            }
            """,
            {"id": order_id},
        )

    async def update_order_note(self, order_id: str, note: str) -> JsonObject:
        return await self.graphql(
            """
            mutation EcomAgentOrderUpdate($input: OrderInput!) {
              orderUpdate(input: $input) {
                order { id note }
                userErrors { field message }
              }
            }
            """,
            {"input": {"id": order_id, "note": note}},
        )

    async def create_refund(
        self,
        *,
        order_id: str,
        note: str | None,
        notify_customer: bool,
        refund_line_items: list[JsonObject],
        transactions: list[JsonObject],
        shipping: JsonObject | None,
    ) -> JsonObject:
        payload: JsonObject = {
            "orderId": order_id,
            "notify": notify_customer,
            "refundLineItems": refund_line_items,
            "transactions": transactions,
        }
        if note is not None:
            payload["note"] = note
        if shipping is not None:
            payload["shipping"] = shipping
        return await self.graphql(
            """
            mutation EcomAgentRefundCreate($input: RefundInput!) {
              refundCreate(input: $input) {
                refund { id totalRefundedSet { shopMoney { amount currencyCode } } }
                userErrors { field message }
              }
            }
            """,
            {"input": payload},
        )

    async def cancel_order(
        self,
        *,
        order_id: str,
        reason: str,
        refund: bool,
        restock: bool,
        notify_customer: bool,
        staff_note: str | None,
    ) -> JsonObject:
        return await self.graphql(
            """
            mutation EcomAgentOrderCancel($orderId: ID!, $reason: OrderCancelReason!,
                                          $refund: Boolean!, $restock: Boolean!,
                                          $notifyCustomer: Boolean,
                                          $staffNote: String) {
              orderCancel(orderId: $orderId, reason: $reason, refund: $refund,
                          restock: $restock, notifyCustomer: $notifyCustomer,
                          staffNote: $staffNote) {
                job { id done }
                orderCancelUserErrors { field message code }
              }
            }
            """,
            {
                "orderId": order_id,
                "reason": reason,
                "refund": refund,
                "restock": restock,
                "notifyCustomer": notify_customer,
                "staffNote": staff_note,
            },
        )

    async def hold_fulfillment_order(
        self,
        *,
        fulfillment_order_id: str,
        reason: str,
        reason_notes: str,
    ) -> JsonObject:
        return await self.graphql(
            """
            mutation EcomAgentFulfillmentHold($fulfillmentHold: FulfillmentOrderHoldInput!,
                                             $id: ID!) {
              fulfillmentOrderHold(fulfillmentHold: $fulfillmentHold, id: $id) {
                fulfillmentOrder { id status requestStatus }
                userErrors { field message }
              }
            }
            """,
            {
                "id": fulfillment_order_id,
                "fulfillmentHold": {"reason": reason, "reasonNotes": reason_notes},
            },
        )


@tool("shopify_get_order", args_schema=ShopifyGetOrderRequest)
async def shopify_get_order(
    merchant_id: UUID,
    case_id: UUID,
    order_id: str,
    shop_domain: str | None = None,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Fetch a Shopify order snapshot for exception analysis."""

    request = ShopifyGetOrderRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        order_id=order_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.get_order(order_id)

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_get_order",
        request=request,
        operation=operation,
    )


@tool("shopify_update_order_note", args_schema=ShopifyUpdateOrderNoteRequest)
async def shopify_update_order_note(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    order_id: str,
    note: str,
    shop_domain: str | None = None,
) -> JsonObject:
    """Replace the note on a Shopify order after human-approved agent action."""

    request = ShopifyUpdateOrderNoteRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        order_id=order_id,
        note=note,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.update_order_note(order_id, note)

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_update_order_note",
        request=request,
        operation=operation,
        write=True,
    )


@tool("shopify_create_refund", args_schema=ShopifyCreateRefundRequest)
async def shopify_create_refund(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    order_id: str,
    note: str | None = None,
    notify_customer: bool = True,
    refund_line_items: list[JsonObject] | None = None,
    transactions: list[JsonObject] | None = None,
    shipping: JsonObject | None = None,
    shop_domain: str | None = None,
) -> JsonObject:
    """Create a Shopify refund. The caller must provide an idempotency key."""

    request = ShopifyCreateRefundRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        order_id=order_id,
        note=note,
        notify_customer=notify_customer,
        refund_line_items=refund_line_items or [],
        transactions=transactions or [],
        shipping=shipping,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.create_refund(
            order_id=order_id,
            note=note,
            notify_customer=notify_customer,
            refund_line_items=request.refund_line_items,
            transactions=request.transactions,
            shipping=shipping,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_create_refund",
        request=request,
        operation=operation,
        write=True,
    )


@tool("shopify_cancel_order", args_schema=ShopifyCancelOrderRequest)
async def shopify_cancel_order(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    order_id: str,
    reason: str = "OTHER",
    refund: bool = False,
    restock: bool = True,
    notify_customer: bool = True,
    staff_note: str | None = None,
    shop_domain: str | None = None,
) -> JsonObject:
    """Cancel a Shopify order after human-approved agent action."""

    request = ShopifyCancelOrderRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        order_id=order_id,
        reason=reason,
        refund=refund,
        restock=restock,
        notify_customer=notify_customer,
        staff_note=staff_note,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.cancel_order(
            order_id=order_id,
            reason=reason,
            refund=refund,
            restock=restock,
            notify_customer=notify_customer,
            staff_note=staff_note,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_cancel_order",
        request=request,
        operation=operation,
        write=True,
    )


@tool("shopify_hold_fulfillment_order", args_schema=ShopifyHoldFulfillmentOrderRequest)
async def shopify_hold_fulfillment_order(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    fulfillment_order_id: str,
    reason_notes: str,
    reason: str = "OTHER",
    shop_domain: str | None = None,
) -> JsonObject:
    """Place a Shopify fulfillment order on hold with an auditable reason."""

    request = ShopifyHoldFulfillmentOrderRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        fulfillment_order_id=fulfillment_order_id,
        reason=reason,
        reason_notes=reason_notes,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.hold_fulfillment_order(
            fulfillment_order_id=fulfillment_order_id,
            reason=reason,
            reason_notes=reason_notes,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_hold_fulfillment_order",
        request=request,
        operation=operation,
        write=True,
    )


def _shop_domain(request: Any, credential: ProviderCredential) -> str:
    if isinstance(request.shop_domain, str) and request.shop_domain:
        return request.shop_domain
    return require_metadata_string(credential, "shop_domain")


def _normalize_shop_domain(shop_domain: str) -> str:
    normalized = shop_domain.removeprefix("https://").removeprefix("http://").strip("/")
    if "/" in normalized or not normalized.endswith(".myshopify.com"):
        raise IntegrationError(
            IntegrationErrorKind.FATAL,
            IntegrationProvider.SHOPIFY,
            "Shopify shop_domain must be a *.myshopify.com host.",
            details={"shop_domain": shop_domain},
        )
    return normalized

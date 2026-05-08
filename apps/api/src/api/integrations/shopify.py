from __future__ import annotations

from typing import Any, cast
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


class ShopifySearchOrdersRequest(ToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    query: str = Field(min_length=1, description="Shopify order search query string.")
    limit: int = Field(default=10, ge=1, le=50)


class ShopifyUpdateOrderNoteRequest(WriteToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    order_id: str = Field(description="Shopify GraphQL Order gid.")
    note: str = Field(min_length=1, description="Replacement note to store on the order.")


class ShopifyUpdateShippingAddressRequest(WriteToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    order_id: str = Field(description="Shopify GraphQL Order gid.")
    shipping_address: JsonObject = Field(description="Replacement Shopify mailing address input.")


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


class ShopifyReleaseFulfillmentHoldRequest(WriteToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    fulfillment_order_id: str = Field(description="Shopify GraphQL FulfillmentOrder gid.")
    hold_ids: list[str] = Field(default_factory=list)
    external_id: str | None = None


class ShopifyApplyOrderEditRequest(WriteToolRequest):
    shop_domain: str | None = Field(default=None, description="Shopify shop domain.")
    order_id: str = Field(description="Shopify GraphQL Order gid.")
    quantity_changes: list[JsonObject] = Field(default_factory=list)
    variant_additions: list[JsonObject] = Field(default_factory=list)
    notify_customer: bool = False
    staff_note: str | None = None


class ShopifyClient:
    def __init__(self, access_token: str, shop_domain: str) -> None:
        settings = get_settings()
        safe_shop_domain = _normalize_shop_domain(shop_domain)
        self._http = ProviderHttpClient(
            IntegrationProvider.SHOPIFY,
            base_url=(
                f"https://{safe_shop_domain}/admin/api/"
                f"{settings.shopify_admin_api_version}"
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
                "graphql.json",
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
                createdAt
                cancelledAt
                cancelReason
                note
                tags
                displayFinancialStatus
                displayFulfillmentStatus
                totalPriceSet { shopMoney { amount currencyCode } }
                customer { id email displayName numberOfOrders }
                shippingAddress {
                  name address1 address2 city province country zip phone
                }
                risk { assessments { riskLevel facts { description sentiment } } }
                fulfillments(first: 20) {
                  id
                  createdAt
                  trackingInfo { company number url }
                }
                fulfillmentOrders(first: 20) {
                  nodes {
                    id status requestStatus supportedActions { action }
                    assignedLocation { name }
                  }
                }
                lineItems(first: 50) {
                  nodes {
                    id
                    sku
                    title
                    quantity
                    refundableQuantity
                    fulfillableQuantity
                  }
                }
              }
            }
            """,
            {"id": _to_order_gid(order_id)},
        )

    async def search_orders(self, *, query: str, limit: int) -> JsonObject:
        return await self.graphql(
            """
            query EcomAgentSearchOrders($query: String!, $first: Int!) {
              orders(first: $first, query: $query) {
                nodes {
                  id
                  name
                  createdAt
                  displayFinancialStatus
                  displayFulfillmentStatus
                  totalPriceSet { shopMoney { amount currencyCode } }
                  email
                  tags
                  customer { id email displayName numberOfOrders }
                  shippingAddress { city province country zip }
                  lineItems(first: 10) {
                    nodes {
                      id
                      sku
                      title
                      quantity
                      variant { id title sku }
                    }
                  }
                }
              }
            }
            """,
            {"query": query, "first": limit},
        )

    async def create_order(
        self,
        *,
        order: JsonObject,
        options: JsonObject | None = None,
    ) -> JsonObject:
        response = await self.graphql(
            """
            mutation EcomAgentOrderCreate(
              $order: OrderCreateOrderInput!,
              $options: OrderCreateOptionsInput
            ) {
              orderCreate(order: $order, options: $options) {
                order {
                  id
                  name
                  email
                  tags
                  displayFinancialStatus
                  displayFulfillmentStatus
                  totalPriceSet { shopMoney { amount currencyCode } }
                }
                userErrors { field message code }
              }
            }
            """,
            {"order": order, "options": options or {}},
        )
        return _mutation_payload(
            response,
            "orderCreate",
            provider=IntegrationProvider.SHOPIFY,
            message="Shopify orderCreate returned user errors.",
        )

    async def update_order_note(self, order_id: str, note: str) -> JsonObject:
        response = await self.graphql(
            """
            mutation EcomAgentOrderUpdate($input: OrderInput!) {
              orderUpdate(input: $input) {
                order { id note }
                userErrors { field message }
              }
            }
            """,
            {"input": {"id": _to_order_gid(order_id), "note": note}},
        )
        _mutation_payload(
            response,
            "orderUpdate",
            provider=IntegrationProvider.SHOPIFY,
            message="Shopify orderUpdate returned user errors while updating the order note.",
        )
        return response

    async def update_shipping_address(
        self,
        *,
        order_id: str,
        shipping_address: JsonObject,
    ) -> JsonObject:
        response = await self.graphql(
            """
            mutation EcomAgentOrderAddressUpdate($input: OrderInput!) {
              orderUpdate(input: $input) {
                order {
                  id
                  shippingAddress {
                    name
                    address1
                    address2
                    city
                    province
                    country
                    zip
                    phone
                  }
                }
                userErrors { field message }
              }
            }
            """,
            {"input": {"id": _to_order_gid(order_id), "shippingAddress": shipping_address}},
        )
        _mutation_payload(
            response,
            "orderUpdate",
            provider=IntegrationProvider.SHOPIFY,
            message="Shopify orderUpdate returned user errors while updating the shipping address.",
        )
        return response

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
            "orderId": _to_order_gid(order_id),
            "notify": notify_customer,
            "refundLineItems": refund_line_items,
            "transactions": transactions,
        }
        if note is not None:
            payload["note"] = note
        if shipping is not None:
            payload["shipping"] = shipping
        response = await self.graphql(
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
        _mutation_payload(
            response,
            "refundCreate",
            provider=IntegrationProvider.SHOPIFY,
            message="Shopify refundCreate returned user errors.",
        )
        return response

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
        refund_method = {"originalPaymentMethodsRefund": True} if refund else None
        response = await self.graphql(
            """
            mutation EcomAgentOrderCancel(
              $orderId: ID!,
              $reason: OrderCancelReason!,
              $restock: Boolean!,
              $notifyCustomer: Boolean,
              $staffNote: String,
              $refundMethod: OrderCancelRefundMethodInput
            ) {
              orderCancel(
                orderId: $orderId,
                reason: $reason,
                restock: $restock,
                notifyCustomer: $notifyCustomer,
                staffNote: $staffNote,
                refundMethod: $refundMethod
              ) {
                job { id done }
                orderCancelUserErrors { field message code }
              }
            }
            """,
            {
                "orderId": _to_order_gid(order_id),
                "reason": reason,
                "restock": restock,
                "notifyCustomer": notify_customer,
                "staffNote": staff_note,
                "refundMethod": refund_method,
            },
        )
        _mutation_payload(
            response,
            "orderCancel",
            provider=IntegrationProvider.SHOPIFY,
            message="Shopify orderCancel returned user errors.",
            error_fields=("orderCancelUserErrors", "userErrors"),
        )
        return response

    async def hold_fulfillment_order(
        self,
        *,
        fulfillment_order_id: str,
        reason: str,
        reason_notes: str,
    ) -> JsonObject:
        response = await self.graphql(
            """
            mutation EcomAgentFulfillmentHold($fulfillmentHold: FulfillmentOrderHoldInput!,
                                             $id: ID!) {
              fulfillmentOrderHold(fulfillmentHold: $fulfillmentHold, id: $id) {
                fulfillmentHold { id }
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
        _mutation_payload(
            response,
            "fulfillmentOrderHold",
            provider=IntegrationProvider.SHOPIFY,
            message="Shopify fulfillmentOrderHold returned user errors.",
        )
        return response

    async def release_fulfillment_hold(
        self,
        *,
        fulfillment_order_id: str,
        hold_ids: list[str] | None = None,
        external_id: str | None = None,
    ) -> JsonObject:
        response = await self.graphql(
            """
            mutation EcomAgentFulfillmentReleaseHold(
              $id: ID!,
              $holdIds: [ID!],
              $externalId: String
            ) {
              fulfillmentOrderReleaseHold(
                id: $id,
                holdIds: $holdIds,
                externalId: $externalId
              ) {
                fulfillmentOrder { id status requestStatus }
                userErrors { field message }
              }
            }
            """,
            {
                "id": fulfillment_order_id,
                "holdIds": hold_ids or None,
                "externalId": external_id,
            },
        )
        _mutation_payload(
            response,
            "fulfillmentOrderReleaseHold",
            provider=IntegrationProvider.SHOPIFY,
            message="Shopify fulfillmentOrderReleaseHold returned user errors.",
        )
        return response

    async def apply_order_edit(
        self,
        *,
        order_id: str,
        quantity_changes: list[JsonObject],
        variant_additions: list[JsonObject],
        notify_customer: bool,
        staff_note: str | None,
    ) -> JsonObject:
        begin = await self.graphql(
            """
            mutation EcomAgentOrderEditBegin($id: ID!) {
              orderEditBegin(id: $id) {
                calculatedOrder {
                  id
                  lineItems(first: 100) {
                    nodes {
                      id
                      title
                      quantity
                      variant { id title sku }
                    }
                  }
                }
                orderEditSession { id }
                userErrors { field message }
              }
            }
            """,
            {"id": _to_order_gid(order_id)},
        )
        payload = ensure_object(IntegrationProvider.SHOPIFY, begin.get("data"))
        begin_result = ensure_object(IntegrationProvider.SHOPIFY, payload.get("orderEditBegin"))
        _raise_user_errors(
            IntegrationProvider.SHOPIFY,
            begin_result.get("userErrors"),
            message="Shopify orderEditBegin returned user errors.",
        )
        calculated_order = ensure_object(IntegrationProvider.SHOPIFY, begin_result.get("calculatedOrder"))
        edit_id = _string(calculated_order.get("id") or _nested(begin_result, ["orderEditSession", "id"]))
        if not edit_id:
            raise IntegrationError(
                IntegrationErrorKind.FATAL,
                IntegrationProvider.SHOPIFY,
                "Shopify did not return a calculated order id for the order edit.",
            )
        calculated_lines = _calculated_line_index(calculated_order)

        for change in quantity_changes:
            line_item_id = _resolve_calculated_line_item_id(change, calculated_lines)
            quantity = _quantity_value(change.get("quantity"))
            restock = bool(change.get("restock", False))
            response = await self.graphql(
                """
                mutation EcomAgentOrderEditSetQuantity(
                  $id: ID!,
                  $lineItemId: ID!,
                  $quantity: Int!,
                  $restock: Boolean
                ) {
                  orderEditSetQuantity(
                    id: $id,
                    lineItemId: $lineItemId,
                    quantity: $quantity,
                    restock: $restock
                  ) {
                    calculatedOrder { id }
                    userErrors { field message }
                  }
                }
                """,
                {
                    "id": edit_id,
                    "lineItemId": line_item_id,
                    "quantity": quantity,
                    "restock": restock,
                },
            )
            set_quantity_result = ensure_object(
                IntegrationProvider.SHOPIFY,
                cast(JsonValue, _nested(response, ["data", "orderEditSetQuantity"], {})),
            )
            _raise_user_errors(
                IntegrationProvider.SHOPIFY,
                set_quantity_result.get("userErrors"),
                message="Shopify orderEditSetQuantity returned user errors.",
            )

        for addition in variant_additions:
            variant_id = _string(addition.get("variant_id"))
            if not variant_id:
                raise IntegrationError(
                    IntegrationErrorKind.FATAL,
                    IntegrationProvider.SHOPIFY,
                    "Each Shopify order edit variant addition requires a variant_id.",
                )
            quantity = _quantity_value(addition.get("quantity"))
            variables: JsonObject = {
                "id": edit_id,
                "variantId": variant_id,
                "quantity": quantity,
                "allowDuplicates": bool(addition.get("allow_duplicates", False)),
            }
            location_id = _string(addition.get("location_id"))
            if location_id:
                variables["locationId"] = location_id
            response = await self.graphql(
                """
                mutation EcomAgentOrderEditAddVariant(
                  $id: ID!,
                  $variantId: ID!,
                  $quantity: Int!,
                  $locationId: ID,
                  $allowDuplicates: Boolean
                ) {
                  orderEditAddVariant(
                    id: $id,
                    variantId: $variantId,
                    quantity: $quantity,
                    locationId: $locationId,
                    allowDuplicates: $allowDuplicates
                  ) {
                    calculatedOrder { id }
                    userErrors { field message }
                  }
                }
                """,
                variables,
            )
            add_variant_result = ensure_object(
                IntegrationProvider.SHOPIFY,
                cast(JsonValue, _nested(response, ["data", "orderEditAddVariant"], {})),
            )
            _raise_user_errors(
                IntegrationProvider.SHOPIFY,
                add_variant_result.get("userErrors"),
                message="Shopify orderEditAddVariant returned user errors.",
            )

        commit = await self.graphql(
            """
            mutation EcomAgentOrderEditCommit(
              $id: ID!,
              $notifyCustomer: Boolean,
              $staffNote: String
            ) {
              orderEditCommit(id: $id, notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
                order { id name }
                successMessages
                userErrors { field message }
              }
            }
            """,
            {
                "id": edit_id,
                "notifyCustomer": notify_customer,
                "staffNote": staff_note,
            },
        )
        commit_result = ensure_object(
            IntegrationProvider.SHOPIFY,
            cast(JsonValue, _nested(commit, ["data", "orderEditCommit"], {})),
        )
        _raise_user_errors(
            IntegrationProvider.SHOPIFY,
            commit_result.get("userErrors"),
            message="Shopify orderEditCommit returned user errors.",
        )
        return {
            "orderEditBegin": begin_result,
            "orderEditCommit": commit_result,
        }


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


@tool("shopify_search_orders", args_schema=ShopifySearchOrdersRequest)
async def shopify_search_orders(
    merchant_id: UUID,
    case_id: UUID,
    query: str,
    limit: int = 10,
    shop_domain: str | None = None,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Search Shopify orders by query for customer-history and fraud-review workflows."""

    request = ShopifySearchOrdersRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        query=query,
        limit=limit,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.search_orders(query=query, limit=limit)

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_search_orders",
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


@tool("shopify_update_shipping_address", args_schema=ShopifyUpdateShippingAddressRequest)
async def shopify_update_shipping_address(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    order_id: str,
    shipping_address: JsonObject,
    shop_domain: str | None = None,
) -> JsonObject:
    """Update the shipping address on a pre-shipment Shopify order."""

    request = ShopifyUpdateShippingAddressRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        order_id=order_id,
        shipping_address=shipping_address,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.update_shipping_address(
            order_id=order_id,
            shipping_address=shipping_address,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_update_shipping_address",
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


@tool("shopify_release_fulfillment_hold", args_schema=ShopifyReleaseFulfillmentHoldRequest)
async def shopify_release_fulfillment_hold(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    fulfillment_order_id: str,
    hold_ids: list[str] | None = None,
    external_id: str | None = None,
    shop_domain: str | None = None,
) -> JsonObject:
    """Release a temporary hold on a Shopify fulfillment order."""

    request = ShopifyReleaseFulfillmentHoldRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        fulfillment_order_id=fulfillment_order_id,
        hold_ids=hold_ids or [],
        external_id=external_id,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.release_fulfillment_hold(
            fulfillment_order_id=fulfillment_order_id,
            hold_ids=hold_ids,
            external_id=external_id,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_release_fulfillment_hold",
        request=request,
        operation=operation,
        write=True,
    )


@tool("shopify_apply_order_edit", args_schema=ShopifyApplyOrderEditRequest)
async def shopify_apply_order_edit(
    merchant_id: UUID,
    case_id: UUID,
    idempotency_key: str,
    order_id: str,
    quantity_changes: list[JsonObject] | None = None,
    variant_additions: list[JsonObject] | None = None,
    notify_customer: bool = False,
    staff_note: str | None = None,
    shop_domain: str | None = None,
) -> JsonObject:
    """Apply a staged Shopify order edit for pre-shipment item add/remove/swap requests."""

    request = ShopifyApplyOrderEditRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key=idempotency_key,
        shop_domain=shop_domain,
        order_id=order_id,
        quantity_changes=quantity_changes or [],
        variant_additions=variant_additions or [],
        notify_customer=notify_customer,
        staff_note=staff_note,
    )

    async def operation(credential: ProviderCredential) -> JsonValue:
        client = ShopifyClient(credential.access_token, _shop_domain(request, credential))
        return await client.apply_order_edit(
            order_id=order_id,
            quantity_changes=request.quantity_changes,
            variant_additions=request.variant_additions,
            notify_customer=notify_customer,
            staff_note=staff_note,
        )

    return await run_tool_with_session(
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_apply_order_edit",
        request=request,
        operation=operation,
        write=True,
    )


def _shop_domain(request: Any, credential: ProviderCredential) -> str:
    if isinstance(request.shop_domain, str) and request.shop_domain:
        return request.shop_domain
    return require_metadata_string(credential, "shop_domain")


def _to_order_gid(order_id: str) -> str:
    if order_id.startswith("gid://"):
        return order_id
    return f"gid://shopify/Order/{order_id}"


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


def _raise_user_errors(
    provider: IntegrationProvider,
    errors: object,
    *,
    message: str,
) -> None:
    if not isinstance(errors, list) or not errors:
        return
    normalized: list[JsonObject] = [item for item in errors if isinstance(item, dict)]
    raise IntegrationError(
        IntegrationErrorKind.FATAL,
        provider,
        message,
        details={"userErrors": normalized},
    )


def _mutation_payload(
    response: JsonObject,
    root_field: str,
    *,
    provider: IntegrationProvider,
    message: str,
    error_fields: tuple[str, ...] = ("userErrors",),
) -> JsonObject:
    payload = ensure_object(provider, cast(JsonValue, _nested(response, ["data", root_field], {})))
    for field_name in error_fields:
        _raise_user_errors(provider, payload.get(field_name), message=message)
    return payload


def _quantity_value(value: object) -> int:
    numeric = _int_from(value)
    if numeric is None or numeric < 0:
        raise IntegrationError(
            IntegrationErrorKind.FATAL,
            IntegrationProvider.SHOPIFY,
            "Shopify order edit quantities must be non-negative integers.",
            details={"quantity": value},
        )
    return numeric


def _calculated_line_index(calculated_order: JsonObject) -> list[JsonObject]:
    raw_nodes = _nested(calculated_order, ["lineItems", "nodes"], [])
    if not isinstance(raw_nodes, list):
        return []
    return [item for item in raw_nodes if isinstance(item, dict)]


def _resolve_calculated_line_item_id(change: JsonObject, calculated_lines: list[JsonObject]) -> str:
    direct_id = _string(change.get("calculated_line_item_id"))
    if direct_id:
        return direct_id

    line_item_id = _string(change.get("line_item_id"))
    if line_item_id.startswith("gid://shopify/CalculatedLineItem/"):
        return line_item_id

    variant_id = _string(change.get("variant_id"))
    if variant_id:
        for line in calculated_lines:
            if _string(_nested(line, ["variant", "id"])) == variant_id:
                resolved = _string(line.get("id"))
                if resolved:
                    return resolved

    title = _string(change.get("title"))
    if title:
        for line in calculated_lines:
            if _string(line.get("title")) == title:
                resolved = _string(line.get("id"))
                if resolved:
                    return resolved

    if line_item_id:
        raise IntegrationError(
            IntegrationErrorKind.FATAL,
            IntegrationProvider.SHOPIFY,
            "Shopify order edit quantity changes require a calculated_line_item_id or a resolvable variant/title mapping.",
            details={"line_item_id": line_item_id},
        )
    raise IntegrationError(
        IntegrationErrorKind.FATAL,
        IntegrationProvider.SHOPIFY,
        "Shopify order edit quantity changes require a line_item_id or calculated_line_item_id.",
        details={"change": change},
    )


def _nested(value: object, path: list[str], default: object = None) -> object:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return default
        current = current.get(part)
    return default if current is None else current


def _string(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _int_from(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None

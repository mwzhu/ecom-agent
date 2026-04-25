from __future__ import annotations

from langchain_core.tools import BaseTool

from api.integrations.gmail import gmail_get_thread, gmail_search_threads
from api.integrations.gorgias import (
    gorgias_draft_reply,
    gorgias_get_ticket,
    gorgias_search_customer,
)
from api.integrations.shipbob import shipbob_get_order, shipbob_get_shipment, shipbob_hold_order
from api.integrations.shipstation import (
    shipstation_get_order,
    shipstation_get_shipment,
    shipstation_hold_order,
)
from api.integrations.shopify import (
    shopify_cancel_order,
    shopify_create_refund,
    shopify_get_order,
    shopify_hold_fulfillment_order,
    shopify_update_order_note,
)
from api.integrations.stripe import (
    stripe_create_refund,
    stripe_get_charge,
    stripe_get_dispute,
    stripe_list_disputes,
)

INTEGRATION_TOOLS: list[BaseTool] = [
    shopify_get_order,
    shopify_update_order_note,
    shopify_cancel_order,
    shopify_create_refund,
    shopify_hold_fulfillment_order,
    stripe_get_charge,
    stripe_get_dispute,
    stripe_list_disputes,
    stripe_create_refund,
    gorgias_get_ticket,
    gorgias_search_customer,
    gorgias_draft_reply,
    shipbob_get_order,
    shipbob_get_shipment,
    shipbob_hold_order,
    shipstation_get_order,
    shipstation_get_shipment,
    shipstation_hold_order,
    gmail_get_thread,
    gmail_search_threads,
]

INTEGRATION_TOOLS_BY_NAME: dict[str, BaseTool] = {tool.name: tool for tool in INTEGRATION_TOOLS}

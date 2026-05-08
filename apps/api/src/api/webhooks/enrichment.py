from __future__ import annotations

import logging
from uuid import UUID

from api.integrations import IntegrationError, IntegrationProvider, IntegrationRepository
from api.integrations.gorgias import GorgiasClient
from api.integrations.shopify import ShopifyClient

JsonObject = dict[str, object]
logger = logging.getLogger(__name__)


async def enrich_webhook_payload(
    *,
    merchant_id: UUID,
    provider: IntegrationProvider,
    payload: JsonObject,
    repository: IntegrationRepository,
) -> JsonObject:
    if provider is not IntegrationProvider.SHOPIFY:
        return payload
    enriched = dict(payload)
    await _enrich_shopify_order(merchant_id=merchant_id, payload=enriched, repository=repository)
    await _enrich_gorgias_ticket(merchant_id=merchant_id, payload=enriched, repository=repository)
    return enriched


async def _enrich_shopify_order(
    *,
    merchant_id: UUID,
    payload: JsonObject,
    repository: IntegrationRepository,
) -> None:
    order_id = _string(payload.get("admin_graphql_api_id") or payload.get("id"))
    if not order_id:
        return
    try:
        credential = await repository.get_credential(merchant_id, IntegrationProvider.SHOPIFY)
        shop_domain = _string(credential.metadata.get("shop_domain"))
        if not shop_domain:
            return
        snapshot = await ShopifyClient(credential.access_token, shop_domain).get_order(order_id)
    except IntegrationError as exc:
        logger.info("Shopify webhook enrichment skipped: %s", exc.normalized.message)
        return
    except Exception as exc:  # noqa: BLE001 - enrichment must not block ingestion.
        logger.info("Shopify webhook enrichment skipped: %s", exc)
        return

    order = _object_at(snapshot, ("data", "order"))
    if not order:
        return
    fulfillment_nodes = _list_at(order, ("fulfillmentOrders", "nodes"))
    if fulfillment_nodes:
        payload["fulfillment_orders"] = fulfillment_nodes
    line_nodes = _list_at(order, ("lineItems", "nodes"))
    if line_nodes:
        payload["line_items_graphql"] = line_nodes
    payload["shopify_admin_snapshot"] = order


async def _enrich_gorgias_ticket(
    *,
    merchant_id: UUID,
    payload: JsonObject,
    repository: IntegrationRepository,
) -> None:
    email = _string(payload.get("email") or payload.get("contact_email"))
    order_name = _string(payload.get("name") or payload.get("order_number"))
    if not email and not order_name:
        return
    try:
        credential = await repository.get_credential(merchant_id, IntegrationProvider.GORGIAS)
        account_domain = _string(
            credential.metadata.get("account_domain") or credential.metadata.get("gorgias_domain")
        )
        if not account_domain:
            return
        ticket = await GorgiasClient(
            credential.access_token,
            account_domain,
            username=_optional_string(credential.metadata.get("username")),
            auth_scheme=_optional_string(credential.metadata.get("auth_scheme")),
        ).find_ticket_for_order(customer_email=email, order_name=order_name)
    except IntegrationError as exc:
        logger.info("Gorgias ticket enrichment skipped: %s", exc.normalized.message)
        return
    except Exception as exc:  # noqa: BLE001 - enrichment must not block ingestion.
        logger.info("Gorgias ticket enrichment skipped: %s", exc)
        return
    if not ticket:
        return
    payload["ticket"] = {
        "id": ticket.get("id"),
        "subject": ticket.get("subject"),
        "status": ticket.get("status"),
        "customer": ticket.get("customer"),
        "excerpt": ticket.get("excerpt"),
        "external_id": ticket.get("external_id"),
    }


def _object_at(value: object, path: tuple[str, ...]) -> JsonObject:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _list_at(value: object, path: tuple[str, ...]) -> list[JsonObject]:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return []
        current = current.get(key)
    return [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []


def _string(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

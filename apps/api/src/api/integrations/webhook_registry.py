from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from api.config import Settings
from api.db.models import WebhookRegistrationStatus
from api.integrations.base import (
    IntegrationProvider,
    IntegrationRepository,
    JsonObject,
    ProviderCredential,
)
from api.integrations.gorgias import GorgiasClient
from api.integrations.shopify import ShopifyClient
from api.integrations.stripe import StripeClient

REQUIRED_WEBHOOK_TOPICS: dict[IntegrationProvider, tuple[str, ...]] = {
    IntegrationProvider.SHOPIFY: (
        "orders/create",
        "orders/updated",
        "orders/cancelled",
        "refunds/create",
        "fulfillment_events/create",
    ),
    IntegrationProvider.STRIPE: (
        "charge.dispute.created",
        "charge.dispute.updated",
        "charge.dispute.closed",
        "charge.refunded",
        "refund.created",
        "refund.failed",
        "payment_intent.payment_failed",
        "payment_intent.succeeded",
    ),
    IntegrationProvider.GORGIAS: (
        "ticket-created",
        "ticket-message-created",
        "ticket-updated",
    ),
}


@dataclass(frozen=True)
class RegisteredWebhook:
    topic: str
    external_webhook_id: str | None
    signing_secret_ref: str | None
    status: WebhookRegistrationStatus
    verified: bool


async def ensure_provider_webhooks(
    *,
    merchant_id: UUID,
    provider: IntegrationProvider,
    credential: ProviderCredential,
    repository: IntegrationRepository,
    settings: Settings,
) -> list[RegisteredWebhook]:
    callback_url = f"{settings.api_base_url}/v1/webhooks/{provider.value}"
    registered: list[RegisteredWebhook] = []
    for topic in REQUIRED_WEBHOOK_TOPICS.get(provider, ()):
        webhook = (
            await _register_live(provider, credential, topic, callback_url, settings)
            if settings.provider_webhook_registration_mode == "live"
            else RegisteredWebhook(
                topic=topic,
                external_webhook_id=None,
                signing_secret_ref=_default_signing_secret_ref(provider),
                status=WebhookRegistrationStatus.PENDING,
                verified=False,
            )
        )
        await repository.upsert_webhook_registration(
            merchant_id=merchant_id,
            provider=provider,
            topic=topic,
            callback_url=callback_url,
            external_webhook_id=webhook.external_webhook_id,
            signing_secret_ref=webhook.signing_secret_ref,
            status=webhook.status,
            verified=webhook.verified,
        )
        registered.append(webhook)
    return registered


async def _register_live(
    provider: IntegrationProvider,
    credential: ProviderCredential,
    topic: str,
    callback_url: str,
    settings: Settings,
) -> RegisteredWebhook:
    if provider is IntegrationProvider.SHOPIFY:
        shop_domain = _metadata_string(credential, "shop_domain")
        topic_name = topic.replace("/", "_").upper()
        client = ShopifyClient(credential.access_token, shop_domain)
        existing = await _find_shopify_webhook_subscription(
            client,
            topic=topic_name,
            callback_url=callback_url,
        )
        if existing:
            return RegisteredWebhook(
                topic=topic,
                external_webhook_id=existing,
                signing_secret_ref="settings.shopify_webhook_secret",
                status=WebhookRegistrationStatus.ACTIVE,
                verified=True,
            )
        result = await client.graphql(
            """
            mutation EcomAgentWebhookSubscriptionCreate(
              $topic: WebhookSubscriptionTopic!,
              $webhookSubscription: WebhookSubscriptionInput!
            ) {
              webhookSubscriptionCreate(
                topic: $topic,
                webhookSubscription: $webhookSubscription
              ) {
                webhookSubscription { id endpoint { __typename } }
                userErrors { field message }
              }
            }
            """,
            {
                "topic": topic_name,
                "webhookSubscription": {
                    "callbackUrl": callback_url,
                    "format": "JSON",
                },
            },
        )
        payload = _object_at(result, ("data", "webhookSubscriptionCreate"))
        subscription = _object_at(payload, ("webhookSubscription",))
        return RegisteredWebhook(
            topic=topic,
            external_webhook_id=_string(subscription.get("id")),
            signing_secret_ref="settings.shopify_webhook_secret",
            status=WebhookRegistrationStatus.ACTIVE,
            verified=True,
        )
    if provider is IntegrationProvider.STRIPE:
        # Stripe endpoint creation returns one endpoint for all enabled events.
        payload = await StripeClient(credential.access_token).create_webhook_endpoint(
            callback_url=callback_url,
            enabled_events=list(REQUIRED_WEBHOOK_TOPICS[IntegrationProvider.STRIPE]),
        )
        secret = _string(payload.get("secret"))
        return RegisteredWebhook(
            topic=topic,
            external_webhook_id=_string(payload.get("id")),
            signing_secret_ref=secret or "stripe.webhook_endpoint_secret",
            status=WebhookRegistrationStatus.ACTIVE,
            verified=True,
        )
    if provider is IntegrationProvider.GORGIAS:
        account_domain = _metadata_string(credential, "account_domain")
        client = GorgiasClient(
            credential.access_token,
            account_domain,
            username=_optional_metadata_string(credential, "username"),
            auth_scheme=_optional_metadata_string(credential, "auth_scheme"),
        )
        existing = await _find_gorgias_http_integrations(
            client,
            topic=topic,
            callback_url=callback_url,
        )
        primary = existing[0] if existing else None
        if existing:
            payload = await client.update_http_integration(
                integration_id=primary,
                topic=topic,
                callback_url=callback_url,
                webhook_secret=settings.gorgias_webhook_secret,
            )
            for duplicate in existing[1:]:
                await client.delete_integration(duplicate)
        else:
            payload = await client.create_http_integration(
                topic=topic,
                callback_url=callback_url,
                webhook_secret=settings.gorgias_webhook_secret,
        )
        return RegisteredWebhook(
            topic=topic,
            external_webhook_id=_string(payload.get("id")) or primary,
            signing_secret_ref="settings.gorgias_webhook_secret",
            status=WebhookRegistrationStatus.ACTIVE,
            verified=True,
        )
    return RegisteredWebhook(
        topic=topic,
        external_webhook_id=None,
        signing_secret_ref=None,
        status=WebhookRegistrationStatus.PENDING,
        verified=False,
    )


async def _find_shopify_webhook_subscription(
    client: ShopifyClient,
    *,
    topic: str,
    callback_url: str,
) -> str | None:
    result = await client.graphql(
        """
        query EcomAgentWebhookSubscriptions($first: Int!, $topics: [WebhookSubscriptionTopic!]) {
          webhookSubscriptions(first: $first, topics: $topics) {
            nodes {
              id
              topic
              endpoint {
                __typename
                ... on WebhookHttpEndpoint {
                  callbackUrl
                }
              }
            }
          }
        }
        """,
        {"first": 100, "topics": [topic]},
    )
    nodes = _list_at(result, ("data", "webhookSubscriptions", "nodes"))
    for node in nodes:
        endpoint = _object_at(node, ("endpoint",))
        if endpoint.get("callbackUrl") == callback_url:
            return _string(node.get("id"))
    return None


async def _find_gorgias_http_integrations(
    client: GorgiasClient,
    *,
    topic: str,
    callback_url: str,
) -> list[str]:
    result = await client.list_integrations(limit=100)
    rows = result.get("data")
    if not isinstance(rows, list):
        return []
    expected_name = f"Ecom Agent {topic}"
    matches: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        http_config = _object_at(row, ("http",))
        name_matches = row.get("name") == expected_name
        url_matches = http_config.get("url") == callback_url
        triggers = http_config.get("triggers")
        trigger_matches = (
            isinstance(triggers, dict)
            and triggers.get(_gorgias_trigger_key(topic)) is True
        )
        if name_matches or (url_matches and trigger_matches):
            integration_id = _string(row.get("id"))
            if integration_id:
                matches.append(integration_id)
    return matches


def _default_signing_secret_ref(provider: IntegrationProvider) -> str | None:
    return {
        IntegrationProvider.SHOPIFY: "settings.shopify_webhook_secret",
        IntegrationProvider.STRIPE: "settings.stripe_webhook_secret",
        IntegrationProvider.GORGIAS: "settings.gorgias_webhook_secret",
    }.get(provider)


def _gorgias_trigger_key(topic: str) -> str:
    return topic.replace("-", "_")


def _metadata_string(credential: ProviderCredential, key: str) -> str:
    value = credential.metadata.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Credential metadata is missing required field {key!r}.")
    return value


def _optional_metadata_string(credential: ProviderCredential, key: str) -> str | None:
    value = credential.metadata.get(key)
    return value if isinstance(value, str) and value else None


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


def _string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int):
        return str(value)
    return None

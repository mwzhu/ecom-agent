#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps/api/src"))

from api.config import get_settings  # noqa: E402
from api.db.session import get_sessionmaker  # noqa: E402
from api.integrations.base import (  # noqa: E402
    IntegrationError,
    IntegrationProvider,
    ProviderCredential,
    SqlAlchemyIntegrationRepository,
)
from api.integrations.gorgias import GorgiasClient  # noqa: E402
from api.integrations.shopify import ShopifyClient  # noqa: E402

DEFAULT_MERCHANT_ID = UUID("00000000-0000-0000-0000-000000000001")
JsonObject = dict[str, object]


@dataclass(frozen=True)
class DemoScenario:
    key: str
    subject: str
    body_template: str
    order_note: str
    item_title: str
    item_price: str


SCENARIOS: tuple[DemoScenario, ...] = (
    DemoScenario(
        key="address_change_request",
        subject="Please update my shipping address for {order_name}",
        body_template=(
            "Hi, I just placed order {order_name}. Can you change the shipping address "
            "to 515 Valencia St, San Francisco, CA 94110 before it ships?"
        ),
        order_note="Real demo: customer requested an address change before fulfillment.",
        item_title="Cloud Cotton Hoodie",
        item_price="84.00",
    ),
    DemoScenario(
        key="order_cancellation_request",
        subject="Cancel my order {order_name}",
        body_template=(
            "Hi support, I ordered the wrong size on {order_name}. Please cancel this "
            "order before the warehouse ships it."
        ),
        order_note="Real demo: customer requested cancellation before fulfillment.",
        item_title="Everyday Canvas Tote",
        item_price="42.00",
    ),
    DemoScenario(
        key="item_change_request",
        subject="Can I swap an item on {order_name}?",
        body_template=(
            "I need help with {order_name}. Please swap the Cloud Cotton Hoodie for a "
            "larger size if this has not shipped yet."
        ),
        order_note="Real demo: customer requested an item change before fulfillment.",
        item_title="Cloud Cotton Hoodie",
        item_price="84.00",
    ),
    DemoScenario(
        key="wismo",
        subject="Where is my order {order_name}?",
        body_template=(
            "Could you check the status of {order_name}? I expected a tracking update "
            "by now and have not received one."
        ),
        order_note="Real demo: customer asked for order/shipping status.",
        item_title="Desk Ritual Kit",
        item_price="58.00",
    ),
)


@dataclass(frozen=True)
class DemoObject:
    scenario: str
    order_id: str
    order_name: str
    customer_email: str
    ticket_id: str | None


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create real Shopify orders and real Gorgias tickets for end-to-end demo testing. "
            "Provider webhooks should then fill the operator console through the normal pipeline."
        )
    )
    parser.add_argument("--merchant-id", type=UUID, default=DEFAULT_MERCHANT_ID)
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--run-tag", default="")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually create objects in Shopify and Gorgias. Without this, only prints the plan.",
    )
    args = parser.parse_args()

    if args.count < 1:
        raise SystemExit("--count must be at least 1")

    run_tag = args.run_tag or f"real-demo-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    if not args.execute:
        print("Dry run. Re-run with --execute to create provider objects.")
        print(f"merchant_id: {args.merchant_id}")
        print(f"run_tag: {run_tag}")
        print(f"objects_to_create: {args.count} Shopify orders + {args.count} Gorgias tickets")
        print("scenarios: " + ", ".join(scenario.key for scenario in SCENARIOS))
        return

    settings = get_settings()
    sessionmaker = get_sessionmaker(settings)
    async with sessionmaker() as session:
        async with session.begin():
            repository = SqlAlchemyIntegrationRepository(session, settings)
            await repository.set_merchant_scope(args.merchant_id)
            shopify_credential = await repository.get_credential(
                args.merchant_id,
                IntegrationProvider.SHOPIFY,
            )
            gorgias_credential = await repository.get_credential(
                args.merchant_id,
                IntegrationProvider.GORGIAS,
            )

    created = await create_demo_objects(
        merchant_id=args.merchant_id,
        shopify_credential=shopify_credential,
        gorgias_credential=gorgias_credential,
        count=args.count,
        run_tag=run_tag,
    )

    print(f"run_tag: {run_tag}")
    print(f"created: {len(created)}")
    for item in created:
        ticket = item.ticket_id or "ticket creation skipped/failed"
        print(
            f"- {item.scenario}: {item.order_name} ({item.order_id}) "
            f"{item.customer_email} ticket={ticket}"
        )
    print("")
    print("Next: wait for Shopify/Gorgias webhooks, then refresh the operator console.")
    print("If cases do not appear, check webhook registration mode and provider webhook delivery logs.")


async def create_demo_objects(
    *,
    merchant_id: UUID,
    shopify_credential: ProviderCredential,
    gorgias_credential: ProviderCredential,
    count: int,
    run_tag: str,
) -> list[DemoObject]:
    shop_domain = _metadata_string(shopify_credential, "shop_domain")
    account_domain = _metadata_string(gorgias_credential, "account_domain")
    shopify = ShopifyClient(shopify_credential.access_token, shop_domain)
    gorgias = GorgiasClient(
        gorgias_credential.access_token,
        account_domain,
        username=_optional_metadata_string(gorgias_credential, "username"),
        auth_scheme=_optional_metadata_string(gorgias_credential, "auth_scheme"),
    )

    created: list[DemoObject] = []
    for index in range(count):
        scenario = SCENARIOS[index % len(SCENARIOS)]
        sequence = index + 1
        customer_email = f"flowlabs-demo+{run_tag}-{sequence}@example.com"
        order_input = shopify_order_input(
            merchant_id=merchant_id,
            scenario=scenario,
            customer_email=customer_email,
            sequence=sequence,
            run_tag=run_tag,
        )
        try:
            order_payload = await shopify.create_order(order=order_input)
        except IntegrationError as exc:
            print(f"Shopify order create failed for {scenario.key} #{sequence}.")
            print(json.dumps(exc.normalized.model_dump(mode="json"), indent=2, sort_keys=True))
            print("order input:")
            print(json.dumps(order_input, indent=2, sort_keys=True))
            raise SystemExit(1) from exc
        order = _object_at(order_payload, ("order",))
        order_id = _string(order.get("id"))
        order_name = _string(order.get("name")) or f"order-{sequence}"
        ticket_id: str | None = None
        try:
            ticket = await gorgias.create_ticket(
                subject=scenario.subject.format(order_name=order_name),
                customer_email=customer_email,
                body_text=scenario.body_template.format(order_name=order_name),
                external_id=f"{run_tag}:{scenario.key}:{sequence}:{uuid4().hex}",
            )
            ticket_id = _string(ticket.get("id")) or _string(_object_at(ticket, ("ticket",)).get("id"))
        except IntegrationError as exc:
            print(f"Gorgias ticket create failed for {order_name}: {exc.normalized.message}")
        created.append(
            DemoObject(
                scenario=scenario.key,
                order_id=order_id,
                order_name=order_name,
                customer_email=customer_email,
                ticket_id=ticket_id,
            )
        )
    return created


def shopify_order_input(
    *,
    merchant_id: UUID,
    scenario: DemoScenario,
    customer_email: str,
    sequence: int,
    run_tag: str,
) -> JsonObject:
    return {
        "email": customer_email,
        "currency": "USD",
        "financialStatus": "PAID",
        "lineItems": [
            {
                "title": scenario.item_title,
                "quantity": 1 + (sequence % 2),
                "priceSet": {
                    "shopMoney": {"amount": scenario.item_price, "currencyCode": "USD"},
                },
                "requiresShipping": True,
                "sku": f"DEMO-{scenario.key[:8].upper()}-{sequence:04d}",
                "taxable": False,
            }
        ],
        "note": f"{scenario.order_note} run_tag={run_tag}",
        "transactions": [
            {
                "kind": "SALE",
                "status": "SUCCESS",
                "amountSet": {
                    "shopMoney": {
                        "amount": str(
                            float(scenario.item_price) * float(1 + (sequence % 2))
                        ),
                        "currencyCode": "USD",
                    }
                },
            }
        ],
        "shippingAddress": {
            "firstName": "Demo",
            "lastName": f"Customer {sequence}",
            "address1": "1 Market St",
            "city": "San Francisco",
            "province": "CA",
            "country": "US",
            "zip": "94105",
            "phone": "+14155550100",
        },
        "tags": ["flowlabs-demo", _shopify_tag(run_tag), _shopify_tag(scenario.key)],
    }


def _metadata_string(credential: ProviderCredential, key: str) -> str:
    value = credential.metadata.get(key)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{credential.provider.value} credential is missing metadata {key!r}")
    return value


def _optional_metadata_string(credential: ProviderCredential, key: str) -> str | None:
    value = credential.metadata.get(key)
    return value if isinstance(value, str) and value else None


def _object_at(value: object, path: tuple[str, ...]) -> dict[str, object]:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _string(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _shopify_tag(value: str) -> str:
    return value.replace("_", "-").replace(":", "-")[:40]


if __name__ == "__main__":
    asyncio.run(main())

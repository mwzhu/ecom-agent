#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps/api/src"))

from api.config import get_settings  # noqa: E402
from api.db.session import get_sessionmaker  # noqa: E402
from api.integrations.base import (  # noqa: E402
    IntegrationError,
    IntegrationProvider,
    SqlAlchemyIntegrationRepository,
)
from api.integrations.webhook_registry import ensure_provider_webhooks  # noqa: E402

DEFAULT_MERCHANT_ID = UUID("00000000-0000-0000-0000-000000000001")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live-register Shopify/Gorgias provider webhooks for an existing merchant."
    )
    parser.add_argument("--merchant-id", type=UUID, default=DEFAULT_MERCHANT_ID)
    parser.add_argument(
        "--providers",
        default="shopify,gorgias",
        help="Comma-separated providers to register. Default: shopify,gorgias",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually create/update webhooks at the providers. Without this, only prints the plan.",
    )
    args = parser.parse_args()

    providers = [
        IntegrationProvider(value.strip())
        for value in args.providers.split(",")
        if value.strip()
    ]
    settings = get_settings().model_copy(update={"provider_webhook_registration_mode": "live"})

    if not args.execute:
        print("Dry run. Re-run with --execute to register webhooks at providers.")
        print(f"merchant_id: {args.merchant_id}")
        print(f"callback_base: {settings.api_base_url}/v1/webhooks")
        print("providers: " + ", ".join(provider.value for provider in providers))
        return

    failures: list[str] = []
    for provider in providers:
        sessionmaker = get_sessionmaker(settings)
        try:
            async with sessionmaker() as session:
                async with session.begin():
                    repository = SqlAlchemyIntegrationRepository(session, settings)
                    await repository.set_merchant_scope(args.merchant_id)
                    credential = await repository.get_credential(args.merchant_id, provider)
                    registrations = await ensure_provider_webhooks(
                        merchant_id=args.merchant_id,
                        provider=provider,
                        credential=credential,
                        repository=repository,
                        settings=settings,
                    )
                    print(f"{provider.value}:")
                    for registration in registrations:
                        print(
                            f"- {registration.topic}: status={registration.status.value} "
                            f"id={registration.external_webhook_id or '-'} "
                            f"verified={registration.verified}"
                        )
        except IntegrationError as exc:
            message = exc.normalized.message
            details = exc.normalized.details
            if isinstance(details.get("response"), dict):
                message = str(details["response"])
            failures.append(f"{provider.value}: {message}")
            print(f"{provider.value}: failed")
            print(f"- {message}")
        except Exception as exc:  # noqa: BLE001 - CLI should report provider-specific failures.
            failures.append(f"{provider.value}: {exc}")
            print(f"{provider.value}: failed")
            print(f"- {exc}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

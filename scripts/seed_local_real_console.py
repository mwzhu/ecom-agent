#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from uuid import UUID

import psycopg

DEFAULT_DATABASE_URL = "postgresql://ecom_agent:ecom_agent@localhost:5432/ecom_agent"
DEFAULT_MERCHANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the local merchant and webhook mapping used by the real console flow."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--merchant-id", type=UUID, default=DEFAULT_MERCHANT_ID)
    parser.add_argument("--org-id", default="org_local_demo")
    parser.add_argument("--merchant-name", default="Local Demo Merchant")
    parser.add_argument("--shop-domain", default="local-test.myshopify.com")
    args = parser.parse_args()

    shop_domain = args.shop_domain.lower()
    with psycopg.connect(args.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into merchants (id, clerk_org_id, name, tier)
                values (%s, %s, %s, 'internal')
                on conflict (clerk_org_id) do update
                  set name = excluded.name
                returning id
                """,
                (args.merchant_id, args.org_id, args.merchant_name),
            )
            merchant_id = cursor.fetchone()[0]
            cursor.execute(
                """
                insert into webhook_sources (merchant_id, provider, external_account_id)
                values (%s, 'shopify', %s)
                on conflict (provider, external_account_id) do update
                  set merchant_id = excluded.merchant_id
                """,
                (merchant_id, shop_domain),
            )

    print(f"Seeded merchant_id={merchant_id}")
    print(f"Mapped Shopify shop domain {shop_domain} to org_id={args.org_id}")


if __name__ == "__main__":
    main()

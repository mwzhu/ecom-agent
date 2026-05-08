#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os

import psycopg

DEFAULT_DATABASE_URL = "postgresql://ecom_agent:ecom_agent@localhost:5432/ecom_agent"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed local synthetic merchants and webhook source mappings."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--shops", type=int, default=1)
    parser.add_argument("--org-prefix", default="org_synthetic")
    parser.add_argument("--merchant-name-prefix", default="Synthetic Merchant")
    args = parser.parse_args()

    if args.shops < 1:
        raise SystemExit("--shops must be at least 1.")

    seeded: list[tuple[int, str, str, str]] = []
    with psycopg.connect(args.database_url) as connection:
        with connection.cursor() as cursor:
            for index in range(1, args.shops + 1):
                clerk_org_id = f"{args.org_prefix}_{index}"
                merchant_name = f"{args.merchant_name_prefix} {index}"
                shop_domain = f"local-test-{index}.myshopify.com"
                stripe_account = f"acct_test_synthetic_{index}"
                gorgias_domain = f"synthetic-{index}.gorgias.com"

                cursor.execute(
                    """
                    insert into merchants (clerk_org_id, name, tier)
                    values (%s, %s, 'internal')
                    on conflict (clerk_org_id) do update
                      set name = excluded.name
                    returning id
                    """,
                    (clerk_org_id, merchant_name),
                )
                merchant_id = cursor.fetchone()[0]
                for provider, external_account_id in (
                    ("shopify", shop_domain),
                    ("stripe", stripe_account),
                    ("gorgias", gorgias_domain),
                ):
                    cursor.execute(
                        """
                        insert into webhook_sources (merchant_id, provider, external_account_id)
                        values (%s, %s, %s)
                        on conflict (provider, external_account_id) do update
                          set merchant_id = excluded.merchant_id
                        """,
                        (merchant_id, provider, external_account_id.lower()),
                    )
                seeded.append((index, str(merchant_id), clerk_org_id, shop_domain))

    print(f"Seeded {len(seeded)} synthetic shop(s).")
    for index, merchant_id, clerk_org_id, shop_domain in seeded:
        print(
            f"shop_index={index} merchant_id={merchant_id} "
            f"org_id={clerk_org_id} shop_domain={shop_domain}"
        )


if __name__ == "__main__":
    main()

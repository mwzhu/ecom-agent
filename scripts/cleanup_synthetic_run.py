#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from collections import Counter
from typing import Any

import psycopg
from psycopg.rows import dict_row
from replay_synthetic_webhooks import DEFAULT_DATABASE_URL, set_scope

JsonObject = dict[str, Any]


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete artifacts for one synthetic run tag.")
    parser.add_argument("--run-tag", required=True)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--yes", action="store_true", help="Actually delete. Default is dry-run.")
    args = parser.parse_args()

    summaries = collect_summaries(database_url=args.database_url, run_tag=args.run_tag)
    totals = Counter[str]()
    for summary in summaries:
        totals.update(summary.counts)

    print(f"run_tag: {args.run_tag}")
    print("dry_run: " + ("false" if args.yes else "true"))
    for table in (
        "webhook_events",
        "cases",
        "case_events",
        "agent_run_executions",
        "tool_calls",
        "eval_corrections",
        "eval_review_items",
    ):
        print(f"{table}: {totals[table]}")

    if not args.yes:
        print("No rows deleted. Re-run with --yes to delete these artifacts.")
        return

    deleted = delete_summaries(
        database_url=args.database_url,
        run_tag=args.run_tag,
        summaries=summaries,
    )
    print("deleted:")
    for table, count in sorted(deleted.items()):
        print(f"- {table}: {count}")


class RunSummary:
    def __init__(self, merchant_id: str, case_ids: list[str], counts: Counter[str]) -> None:
        self.merchant_id = merchant_id
        self.case_ids = case_ids
        self.counts = counts


def collect_summaries(*, database_url: str, run_tag: str) -> list[RunSummary]:
    summaries: list[RunSummary] = []
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        for merchant in all_merchants(connection):
            with connection.cursor() as cursor:
                set_scope(cursor, merchant["id"])
                case_ids = synthetic_case_ids(cursor, run_tag, str(merchant["id"]))
                counts = Counter[str]()
                counts["webhook_events"] = count_webhook_events(cursor, run_tag, str(merchant["id"]))
                counts["cases"] = len(case_ids)
                for table in (
                    "case_events",
                    "agent_run_executions",
                    "tool_calls",
                    "eval_corrections",
                    "eval_review_items",
                ):
                    counts[table] = count_by_case_ids(cursor, table, case_ids)
                if counts["webhook_events"] or counts["cases"]:
                    summaries.append(RunSummary(str(merchant["id"]), case_ids, counts))
    return summaries


def delete_summaries(
    *,
    database_url: str,
    run_tag: str,
    summaries: list[RunSummary],
) -> Counter[str]:
    deleted = Counter[str]()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        for summary in summaries:
            with connection.cursor() as cursor:
                set_scope(cursor, summary.merchant_id)
                for table in (
                    "eval_review_items",
                    "eval_corrections",
                    "tool_calls",
                    "agent_run_executions",
                    "case_events",
                ):
                    deleted[table] += delete_by_case_ids(cursor, table, summary.case_ids)
                deleted["cases"] += delete_cases(cursor, summary.case_ids)
                deleted["webhook_events"] += delete_webhook_events(
                    cursor,
                    run_tag,
                    summary.merchant_id,
                )
    return deleted


def all_merchants(connection: psycopg.Connection[dict[str, Any]]) -> list[JsonObject]:
    with connection.cursor() as cursor:
        cursor.execute("select id::text as id from merchants order by created_at, id")
        return [dict(row) for row in cursor.fetchall()]


def synthetic_case_ids(
    cursor: psycopg.Cursor[dict[str, Any]],
    run_tag: str,
    merchant_id: str,
) -> list[str]:
    cursor.execute(
        """
        select c.id::text
        from webhook_events we
        join cases c
          on c.merchant_id = we.merchant_id
         and c.subject_ref->>'provider' = we.provider
         and c.subject_ref->>'event_id' = we.event_id
        where we.merchant_id = %s::uuid
          and we.payload->'synthetic'->>'run_tag' = %s
        """,
        (merchant_id, run_tag),
    )
    return [str(row["id"]) for row in cursor.fetchall()]


def count_webhook_events(
    cursor: psycopg.Cursor[dict[str, Any]],
    run_tag: str,
    merchant_id: str,
) -> int:
    cursor.execute(
        """
        select count(*) as count
        from webhook_events
        where merchant_id = %s::uuid
          and payload->'synthetic'->>'run_tag' = %s
        """,
        (merchant_id, run_tag),
    )
    return int(cursor.fetchone()["count"])


def count_by_case_ids(
    cursor: psycopg.Cursor[dict[str, Any]],
    table: str,
    case_ids: list[str],
) -> int:
    if not case_ids:
        return 0
    cursor.execute(
        f"select count(*) as count from {table} where case_id = any(%s::uuid[])",
        (case_ids,),
    )
    return int(cursor.fetchone()["count"])


def delete_by_case_ids(
    cursor: psycopg.Cursor[dict[str, Any]],
    table: str,
    case_ids: list[str],
) -> int:
    if not case_ids:
        return 0
    cursor.execute(
        f"delete from {table} where case_id = any(%s::uuid[])",
        (case_ids,),
    )
    return int(cursor.rowcount)


def delete_cases(cursor: psycopg.Cursor[dict[str, Any]], case_ids: list[str]) -> int:
    if not case_ids:
        return 0
    cursor.execute("delete from cases where id = any(%s::uuid[])", (case_ids,))
    return int(cursor.rowcount)


def delete_webhook_events(
    cursor: psycopg.Cursor[dict[str, Any]],
    run_tag: str,
    merchant_id: str,
) -> int:
    cursor.execute(
        """
        delete from webhook_events
        where merchant_id = %s::uuid
          and payload->'synthetic'->>'run_tag' = %s
        """,
        (merchant_id, run_tag),
    )
    return int(cursor.rowcount)


if __name__ == "__main__":
    main()

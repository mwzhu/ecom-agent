#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import Any

import psycopg
from psycopg.rows import dict_row
from replay_synthetic_webhooks import (
    DEFAULT_DATABASE_URL,
    classifier_confusion_matrix,
    json_object,
    query_run_cases,
    set_scope,
)

JsonObject = dict[str, Any]


def main() -> None:
    parser = argparse.ArgumentParser(description="Report on a settled synthetic webhook run.")
    parser.add_argument("--run-tag", required=True)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    args = parser.parse_args()

    cases = query_run_cases(database_url=args.database_url, run_tag=args.run_tag)
    webhook_rows = query_webhook_rows(database_url=args.database_url, run_tag=args.run_tag)
    execution_rows = query_execution_rows(database_url=args.database_url, run_tag=args.run_tag)
    planned_tools = Counter(
        str(call.get("tool"))
        for case in cases
        for call in graph_tool_calls(case.resolution)
        if call.get("tool")
    )
    skipped_count = len(
        {
            str(row.get("case_id"))
            for row in execution_rows
            if row.get("event_kind") == "agent.execution_synthetic_skipped"
            or execution_status(row.get("resolution")) == "synthetic_skipped"
        }
    )

    print(f"run_tag: {args.run_tag}")
    print(f"providers: {sorted({str(row['provider']) for row in webhook_rows})}")
    print("shops/merchants involved:")
    for merchant in sorted(
        {
            (
                str(row["merchant_id"]),
                str(row["merchant_name"]),
                str(row["shop_index"] or ""),
            )
            for row in webhook_rows
        },
        key=lambda item: (item[2], item[1]),
    ):
        print(f"- shop_index={merchant[2] or '?'} merchant_id={merchant[0]} name={merchant[1]}")

    print(f"webhook events by provider: {dict(sorted(Counter(str(row['provider']) for row in webhook_rows).items()))}")
    print(f"cases by type: {dict(sorted(Counter(case.case_type for case in cases).items()))}")
    print(f"cases by status: {dict(sorted(Counter(case.case_status for case in cases).items()))}")
    pending = [case.case_id for case in cases if case.case_status == "pending_approval"]
    failed = [case.case_id for case in cases if case.case_status == "failed"]
    print(f"pending approvals: {len(pending)}")
    if pending:
        print(f"pending approval case ids: {sample_ids(pending)}")
    print(f"failed cases: {len(failed)}")
    if failed:
        print(f"failed case ids: {sample_ids(failed)}")
    print(f"planned tools by tool name: {dict(sorted(planned_tools.items()))}")
    print(f"synthetic skipped executions: {skipped_count}")
    if any(case.expected_exception_types for case in cases):
        print(
            "classifier confusion matrix: "
            + json.dumps(classifier_confusion_matrix(cases), sort_keys=True)
        )


def query_webhook_rows(*, database_url: str, run_tag: str) -> list[JsonObject]:
    rows: list[JsonObject] = []
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select id::text as id, name from merchants order by created_at, id")
            merchants = [dict(row) for row in cursor.fetchall()]
        for merchant in merchants:
            with connection.cursor() as cursor:
                set_scope(cursor, merchant["id"])
                cursor.execute(
                    """
                    select
                      we.provider,
                      we.event_id,
                      we.merchant_id::text as merchant_id,
                      we.payload,
                      we.created_at,
                      we.processed_at
                    from webhook_events we
                    where we.merchant_id = %s::uuid
                      and we.payload->'synthetic'->>'run_tag' = %s
                    order by we.created_at, we.event_id
                    """,
                    (merchant["id"], run_tag),
                )
                for row in cursor.fetchall():
                    synthetic = json_object(json_object(row["payload"]).get("synthetic"))
                    rows.append(
                        {
                            **dict(row),
                            "merchant_name": merchant["name"],
                            "shop_index": synthetic.get("shop_index"),
                        }
                    )
    return rows


def query_execution_rows(*, database_url: str, run_tag: str) -> list[JsonObject]:
    rows: list[JsonObject] = []
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select id::text as id from merchants order by created_at, id")
            merchants = [dict(row) for row in cursor.fetchall()]
        for merchant in merchants:
            with connection.cursor() as cursor:
                set_scope(cursor, merchant["id"])
                cursor.execute(
                    """
                    select
                      c.id::text as case_id,
                      c.resolution,
                      ce.kind as event_kind,
                      are.status as execution_status,
                      are.execution_results
                    from webhook_events we
                    join cases c
                      on c.merchant_id = we.merchant_id
                     and c.subject_ref->>'provider' = we.provider
                     and c.subject_ref->>'event_id' = we.event_id
                    left join case_events ce
                      on ce.case_id = c.id
                     and ce.kind = 'agent.execution_synthetic_skipped'
                    left join agent_run_executions are
                      on are.case_id = c.id
                    where we.merchant_id = %s::uuid
                      and we.payload->'synthetic'->>'run_tag' = %s
                    """,
                    (merchant["id"], run_tag),
                )
                rows.extend(dict(row) for row in cursor.fetchall())
    return rows


def graph_tool_calls(resolution: JsonObject) -> list[JsonObject]:
    graph = json_object(resolution.get("graph"))
    calls = graph.get("tool_calls")
    return [item for item in calls if isinstance(item, dict)] if isinstance(calls, list) else []


def execution_status(resolution: object) -> str:
    execution = json_object(json_object(resolution).get("execution"))
    status = execution.get("status")
    return status if isinstance(status, str) else ""


def sample_ids(ids: list[str], limit: int = 10) -> list[str]:
    if len(ids) <= limit:
        return ids
    return [*ids[:limit], f"... {len(ids) - limit} more"]


if __name__ == "__main__":
    main()

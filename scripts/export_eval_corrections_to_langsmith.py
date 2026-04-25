from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

JsonObject = dict[str, Any]

DEFAULT_DATABASE_URL = "postgresql://ecom_agent:ecom_agent@localhost:5432/ecom_agent"


@dataclass(frozen=True)
class CorrectionExport:
    correction_id: str
    merchant_id: str
    inputs: JsonObject
    outputs: JsonObject
    metadata: JsonObject


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    dataset_name = os.environ.get("LANGSMITH_EVAL_DATASET", "order-exception-v0")
    limit = int(os.environ.get("EVAL_CORRECTIONS_EXPORT_LIMIT", "100"))
    dry_run = os.environ.get("EVAL_CORRECTIONS_DRY_RUN", "").lower() == "true"

    rows = _load_queued_corrections(database_url, limit)
    exports = [_row_to_export(row) for row in rows]
    if not exports:
        print("No queued eval corrections to export.")
        return

    exported = _sync_langsmith_examples(dataset_name, exports, dry_run=dry_run)
    if dry_run:
        print(
            f"Dry run: prepared {len(exported)} eval correction example(s) "
            f"for LangSmith dataset {dataset_name!r}."
        )
        return

    _mark_exported(database_url, exported)
    print(
        f"Exported {len(exported)} eval correction example(s) "
        f"to LangSmith dataset {dataset_name!r}."
    )


def _load_queued_corrections(database_url: str, limit: int) -> list[JsonObject]:
    query = """
        select
          ec.id::text as correction_id,
          ec.case_id::text as case_id,
          ec.merchant_id::text as merchant_id,
          ec.expected_resolution,
          ec.notes,
          ec.created_by,
          ec.created_at::text as correction_created_at,
          c.type as case_type,
          c.status as case_status,
          c.subject_ref,
          c.resolution,
          coalesce(
            (
              select json_agg(
                json_build_object(
                  'kind', ce.kind,
                  'actor', ce.actor,
                  'payload', ce.payload,
                  'langsmith_run_id', ce.langsmith_run_id,
                  'created_at', ce.created_at
                )
                order by ce.created_at asc, ce.id asc
              )
              from case_events ce
              where ce.case_id = ec.case_id
            ),
            '[]'::json
          ) as case_events
        from eval_corrections ec
        join cases c on c.id = ec.case_id
        where ec.status = 'queued'
        order by ec.created_at asc, ec.id asc
        limit %s
    """
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            cursor.execute("select id::text from merchants order by created_at asc, id asc")
            merchant_ids = [str(row["id"]) for row in cursor.fetchall()]
            rows: list[JsonObject] = []
            for merchant_id in merchant_ids:
                remaining = limit - len(rows)
                if remaining <= 0:
                    break
                cursor.execute("select set_config('app.merchant_id', %s, true)", (merchant_id,))
                cursor.execute(query, (remaining,))
                rows.extend(dict(row) for row in cursor.fetchall())
            return rows


def _row_to_export(row: JsonObject) -> CorrectionExport:
    correction_id = str(row["correction_id"])
    inputs = {
        "case_id": row["case_id"],
        "merchant_id": row["merchant_id"],
        "case_type": row["case_type"],
        "case_status": row["case_status"],
        "subject_ref": _json_object(row.get("subject_ref")),
        "agent_resolution": _json_object(row.get("resolution")),
        "case_events": _json_list(row.get("case_events")),
    }
    outputs = {
        "expected_resolution": _json_object(row.get("expected_resolution")),
        "operator_notes": str(row.get("notes") or ""),
    }
    metadata = {
        "source": "eval_corrections",
        "correction_id": correction_id,
        "merchant_id": row["merchant_id"],
        "case_id": row["case_id"],
        "created_by": row["created_by"],
        "created_at": row["correction_created_at"],
    }
    return CorrectionExport(
        correction_id=correction_id,
        merchant_id=str(row["merchant_id"]),
        inputs=inputs,
        outputs=outputs,
        metadata=metadata,
    )


def _sync_langsmith_examples(
    dataset_name: str,
    exports: list[CorrectionExport],
    *,
    dry_run: bool,
) -> list[CorrectionExport]:
    if dry_run:
        return exports

    from langsmith import Client

    client = Client()
    try:
        dataset = client.read_dataset(dataset_name=dataset_name)
    except Exception:  # noqa: BLE001 - missing dataset should create a new one.
        dataset = client.create_dataset(
            dataset_name=dataset_name,
            description="Phase 0 Order Exception Agent scenarios and console corrections.",
        )

    existing_ids = _existing_correction_ids(client, dataset.id)
    exported: list[CorrectionExport] = []
    for export in exports:
        if export.correction_id not in existing_ids:
            client.create_example(
                dataset_id=dataset.id,
                inputs=export.inputs,
                outputs=export.outputs,
                metadata=export.metadata,
            )
        exported.append(export)
    return exported


def _existing_correction_ids(client: Any, dataset_id: object) -> set[str]:
    existing_ids: set[str] = set()
    for example in client.list_examples(dataset_id=dataset_id):
        metadata = getattr(example, "metadata", {}) or {}
        if isinstance(metadata, dict):
            correction_id = metadata.get("correction_id")
            if isinstance(correction_id, str):
                existing_ids.add(correction_id)
    return existing_ids


def _mark_exported(database_url: str, exports: list[CorrectionExport]) -> None:
    if not exports:
        return
    ids_by_merchant: dict[str, list[str]] = {}
    for export in exports:
        ids_by_merchant.setdefault(export.merchant_id, []).append(export.correction_id)
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cursor:
            for merchant_id, correction_ids in ids_by_merchant.items():
                placeholders = ", ".join(["%s"] * len(correction_ids))
                statement = (
                    "update eval_corrections set status = 'exported' "
                    f"where id in ({placeholders})"
                )
                cursor.execute("select set_config('app.merchant_id', %s, true)", (merchant_id,))
                cursor.execute(statement, [UUID(value) for value in correction_ids])
        conn.commit()


def _json_object(value: object) -> JsonObject:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: object) -> list[JsonObject]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []


if __name__ == "__main__":
    main()

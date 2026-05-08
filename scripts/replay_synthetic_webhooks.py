#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from synthetic_webhook_scenarios import (
    COVERAGE_SCENARIO_BY_EXCEPTION,
    IMPLEMENTED_EXCEPTION_TYPES,
    Scenario,
    ScenarioContext,
    scenario_by_id,
    scenarios_for_profile,
)

DEFAULT_API_BASE_URL = "http://localhost:8000"
DEFAULT_DATABASE_URL = "postgresql://ecom_agent:ecom_agent@localhost:5432/ecom_agent"

Fate = Literal["good", "duplicate", "bad_signature", "malformed"]
JsonObject = dict[str, Any]


@dataclass
class RequestJob:
    event_id: str
    scenario_id: str
    provider: str
    intended_fate: Fate
    expected_exception_types: frozenset[str]
    shop_index: int
    payload: JsonObject
    body: bytes
    endpoint: str
    headers: dict[str, str]
    duplicate_of: str | None = None


@dataclass
class SentEvent:
    event_id: str
    scenario_id: str
    provider: str
    intended_fate: Fate
    expected_exception_types: frozenset[str]
    shop_index: int
    status_code: int
    response_payload: JsonObject | None
    response_text: str
    duplicate_of: str | None = None


@dataclass
class SettledCase:
    event_id: str
    provider: str
    merchant_id: str
    case_id: str
    case_type: str
    case_status: str
    scenario_id: str
    expected_exception_types: frozenset[str]
    synthetic: JsonObject
    resolution: JsonObject
    synced: bool


@dataclass
class RunReport:
    run_tag: str
    sent: list[SentEvent]
    settled_cases: list[SettledCase]
    duplicate_case_counts: dict[str, int]
    failures: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> int:
        return sum(
            1
            for event in self.sent
            if event.status_code == 200
            and event.response_payload is not None
            and event.response_payload.get("status") == "accepted"
        )

    @property
    def duplicates(self) -> int:
        return sum(
            1
            for event in self.sent
            if event.status_code == 200
            and event.response_payload is not None
            and event.response_payload.get("status") == "duplicate"
        )

    @property
    def bad_signature_rejections(self) -> int:
        return sum(event.status_code == 401 for event in self.sent)

    @property
    def malformed_rejections(self) -> int:
        return sum(event.status_code == 400 for event in self.sent)

    @property
    def classifier_mismatch_rate(self) -> float:
        good_events = {
            (event.provider, event.event_id): event
            for event in self.sent
            if event.intended_fate == "good"
            and event.status_code == 200
            and event.response_payload is not None
            and event.response_payload.get("status") == "accepted"
        }
        if not good_events:
            return 0.0
        mismatches = 0
        settled_by_key = {
            (case.provider, case.event_id): case
            for case in self.settled_cases
            if (case.provider, case.event_id) in good_events
        }
        for key, event in good_events.items():
            case = settled_by_key.get(key)
            if case is None or case.case_type not in event.expected_exception_types:
                mismatches += 1
        return mismatches / len(good_events)

    @property
    def missing_expected_coverage(self) -> list[str]:
        covered = {case.case_type for case in self.settled_cases}
        return [
            exception_type
            for exception_type in IMPLEMENTED_EXCEPTION_TYPES
            if exception_type not in covered
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay signed synthetic production-like webhooks.")
    parser.add_argument("--api-base-url", default=os.environ.get("API_BASE_URL", DEFAULT_API_BASE_URL))
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument(
        "--profile",
        choices=[
            "mixed",
            "fraud",
            "customer_changes",
            "fulfillment",
            "delivery",
            "stripe",
            "gorgias",
            "chaos",
        ],
        default="mixed",
    )
    parser.add_argument("--seed", type=int)
    parser.add_argument("--run-tag")
    parser.add_argument("--shops", type=int, default=1)
    parser.add_argument("--force-exception-type", action="store_true")
    parser.add_argument("--duplicate-rate", type=float, default=0.05)
    parser.add_argument("--bad-signature-rate", type=float, default=0.02)
    parser.add_argument("--malformed-rate", type=float, default=0.01)
    parser.add_argument("--classifier-mismatch-threshold", type=float, default=0.10)
    parser.add_argument("--settle-timeout-seconds", type=float, default=300)
    parser.add_argument("--settle-poll-seconds", type=float, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.count < 1:
        raise SystemExit("--count must be at least 1.")
    if args.shops < 1:
        raise SystemExit("--shops must be at least 1.")
    if args.profile == "mixed" and args.count < len(IMPLEMENTED_EXCEPTION_TYPES):
        raise SystemExit(
            f"--profile mixed needs at least {len(IMPLEMENTED_EXCEPTION_TYPES)} events."
        )

    explicit_concurrency = any(item == "--concurrency" for item in os.sys.argv[1:])
    if args.profile == "chaos":
        args.duplicate_rate = 0.25
        args.bad_signature_rate = 0.15
        args.malformed_rate = 0.10
        if not explicit_concurrency:
            args.concurrency = max(args.concurrency, 25)

    rng = random.Random(args.seed)
    run_tag = args.run_tag or default_run_tag()
    secrets = WebhookSecrets.from_environment()
    jobs = build_jobs(
        count=args.count,
        profile=args.profile,
        run_tag=run_tag,
        shops=args.shops,
        force_exception_type=args.force_exception_type,
        duplicate_rate=args.duplicate_rate,
        bad_signature_rate=args.bad_signature_rate,
        malformed_rate=args.malformed_rate,
        rng=rng,
        secrets=secrets,
        api_base_url=args.api_base_url,
    )
    report = asyncio.run(
        run_replay(
            jobs=jobs,
            api_base_url=args.api_base_url,
            database_url=args.database_url,
            run_tag=run_tag,
            concurrency=args.concurrency,
            settle_timeout_seconds=args.settle_timeout_seconds,
            settle_poll_seconds=args.settle_poll_seconds,
            classifier_mismatch_threshold=args.classifier_mismatch_threshold,
            require_mixed_coverage=args.profile == "mixed",
        )
    )
    emit_report(report, json_output=args.json)
    if report.failures:
        raise SystemExit(1)


@dataclass(frozen=True)
class WebhookSecrets:
    shopify: str
    stripe: str
    gorgias: str

    @classmethod
    def from_environment(cls) -> WebhookSecrets:
        env = root_env()
        return cls(
            shopify=(
                os.environ.get("SHOPIFY_WEBHOOK_SECRET")
                or os.environ.get("SHOPIFY_CLIENT_SECRET")
                or env.get("SHOPIFY_WEBHOOK_SECRET")
                or env.get("SHOPIFY_CLIENT_SECRET")
                or "local-dev-shopify-webhook-secret"
            ),
            stripe=(
                os.environ.get("STRIPE_WEBHOOK_SECRET")
                or env.get("STRIPE_WEBHOOK_SECRET")
                or "whsec_local_dev"
            ),
            gorgias=(
                os.environ.get("GORGIAS_WEBHOOK_SECRET")
                or env.get("GORGIAS_WEBHOOK_SECRET")
                or "local-dev-gorgias-webhook-secret"
            ),
        )


def build_jobs(
    *,
    count: int,
    profile: str,
    run_tag: str,
    shops: int,
    force_exception_type: bool,
    duplicate_rate: float,
    bad_signature_rate: float,
    malformed_rate: float,
    rng: random.Random,
    secrets: WebhookSecrets,
    api_base_url: str,
) -> list[RequestJob]:
    selected = select_scenarios(profile=profile, count=count, rng=rng)
    run_hash = short_run_hash(run_tag)
    protected_good_positions = (
        set(range(1, len(IMPLEMENTED_EXCEPTION_TYPES) + 1)) if profile == "mixed" else set()
    )
    fates = assign_fates(
        count=count,
        protected_good_positions=protected_good_positions,
        duplicate_rate=duplicate_rate,
        bad_signature_rate=bad_signature_rate,
        malformed_rate=malformed_rate,
        rng=rng,
    )
    jobs: list[RequestJob] = []

    for index, scenario in enumerate(selected, start=1):
        shop_index = ((index - 1) % shops) + 1
        event_id = f"evt_sim_{run_hash}_{index:05d}"
        ctx = ScenarioContext(
            run_tag=run_tag,
            scenario_id=scenario.id,
            sequence=index,
            event_id=event_id,
            order_name=f"#SIM-{run_hash}-{index:04d}",
            shop_index=shop_index,
            shop_domain=f"local-test-{shop_index}.myshopify.com",
            stripe_account=f"acct_test_synthetic_{shop_index}",
            gorgias_domain=f"synthetic-{shop_index}.gorgias.com",
            expected_exception_types=scenario.expected_exception_types,
            force_exception_type=force_exception_type,
        )
        payload = scenario.factory(ctx)
        fate = fates[index]
        if fate == "duplicate" and not jobs:
            fate = "good"
        body = encode_body(payload)
        if fate == "malformed":
            body = malformed_body(run_tag=run_tag, event_id=event_id, scenario_id=scenario.id)
        job = make_request_job(
            scenario=scenario,
            ctx=ctx,
            fate=fate,
            payload=payload,
            body=body,
            secrets=secrets,
            api_base_url=api_base_url,
        )
        jobs.append(job)

    originals = [job for job in jobs if job.intended_fate == "good"]
    duplicate_jobs = [job for job in jobs if job.intended_fate == "duplicate"]
    for offset, duplicate in enumerate(duplicate_jobs):
        original = originals[offset % len(originals)]
        duplicate.event_id = original.event_id
        duplicate.scenario_id = original.scenario_id
        duplicate.provider = original.provider
        duplicate.expected_exception_types = original.expected_exception_types
        duplicate.shop_index = original.shop_index
        duplicate.payload = original.payload
        duplicate.body = original.body
        duplicate.endpoint = original.endpoint
        duplicate.headers = dict(original.headers)
        duplicate.duplicate_of = original.event_id

    return [job for job in jobs if job.intended_fate != "duplicate"] + duplicate_jobs


def assign_fates(
    *,
    count: int,
    protected_good_positions: set[int],
    duplicate_rate: float,
    bad_signature_rate: float,
    malformed_rate: float,
    rng: random.Random,
) -> dict[int, Fate]:
    fates: dict[int, Fate] = {position: "good" for position in range(1, count + 1)}
    eligible = [
        position for position in range(1, count + 1) if position not in protected_good_positions
    ]
    rng.shuffle(eligible)
    cursor = 0
    for fate, rate in (
        ("malformed", malformed_rate),
        ("bad_signature", bad_signature_rate),
        ("duplicate", duplicate_rate),
    ):
        remaining = len(eligible) - cursor
        fate_count = requested_fate_count(rate=rate, eligible_count=len(eligible))
        fate_count = min(fate_count, remaining)
        for position in eligible[cursor : cursor + fate_count]:
            fates[position] = fate
        cursor += fate_count
    return fates


def requested_fate_count(*, rate: float, eligible_count: int) -> int:
    if rate <= 0 or eligible_count <= 0:
        return 0
    count = round(eligible_count * rate)
    return max(1, count)


def select_scenarios(*, profile: str, count: int, rng: random.Random) -> list[Scenario]:
    by_id = scenario_by_id()
    candidates = scenarios_for_profile(profile)
    if not candidates:
        raise SystemExit(f"No synthetic scenarios are registered for profile {profile!r}.")
    selected: list[Scenario] = []
    if profile == "mixed":
        selected.extend(by_id[scenario_id] for scenario_id in COVERAGE_SCENARIO_BY_EXCEPTION.values())
    while len(selected) < count:
        selected.append(weighted_choice(candidates, rng))
    return selected[:count]


def weighted_choice(candidates: list[Scenario], rng: random.Random) -> Scenario:
    weights = {
        "fraud_triage": 1.1,
        "address_change_request": 1.0,
        "item_change_request": 1.0,
        "order_cancellation_request": 0.9,
        "inventory_conflict": 0.8,
        "order_not_picked": 0.8,
        "stuck_in_transit": 0.8,
        "wismo": 1.2,
        "delivered_not_received": 0.8,
        "damaged_in_transit": 0.7,
    }
    numeric_weights = [
        max(weights.get(next(iter(scenario.expected_exception_types)), 1.0), 0.1)
        for scenario in candidates
    ]
    return rng.choices(candidates, weights=numeric_weights, k=1)[0]


def make_request_job(
    *,
    scenario: Scenario,
    ctx: ScenarioContext,
    fate: Fate,
    payload: JsonObject,
    body: bytes,
    secrets: WebhookSecrets,
    api_base_url: str,
) -> RequestJob:
    endpoint = f"{api_base_url.rstrip('/')}/v1/webhooks/{scenario.provider}"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Ecom-Synthetic-Run-Tag": ctx.run_tag,
    }
    if scenario.provider == "shopify":
        headers.update(
            {
                "X-Shopify-Hmac-Sha256": shopify_signature(secrets.shopify, body),
                "X-Shopify-Shop-Domain": ctx.shop_domain,
                "X-Shopify-Topic": str(payload.get("topic") or "orders/updated"),
                "X-Shopify-Webhook-Id": ctx.event_id,
            }
        )
    elif scenario.provider == "stripe":
        headers.update(
            {
                "Stripe-Signature": stripe_signature(secrets.stripe, body),
                "Stripe-Account": ctx.stripe_account,
            }
        )
    elif scenario.provider == "gorgias":
        headers.update(
            {
                "X-Gorgias-Hmac-Sha256": hex_signature(secrets.gorgias, body),
                "X-Gorgias-Domain": ctx.gorgias_domain,
                "X-Gorgias-Event-Id": ctx.event_id,
            }
        )
    if fate == "bad_signature":
        poison_signature(headers, scenario.provider)
    return RequestJob(
        event_id=ctx.event_id,
        scenario_id=scenario.id,
        provider=scenario.provider,
        intended_fate=fate,
        expected_exception_types=scenario.expected_exception_types,
        shop_index=ctx.shop_index,
        payload=payload,
        body=body,
        endpoint=endpoint,
        headers=headers,
    )


async def run_replay(
    *,
    jobs: list[RequestJob],
    api_base_url: str,
    database_url: str,
    run_tag: str,
    concurrency: int,
    settle_timeout_seconds: float,
    settle_poll_seconds: float,
    classifier_mismatch_threshold: float,
    require_mixed_coverage: bool,
) -> RunReport:
    del api_base_url
    non_duplicates = [job for job in jobs if job.intended_fate != "duplicate"]
    duplicates = [job for job in jobs if job.intended_fate == "duplicate"]

    async with httpx.AsyncClient(timeout=30) as client:
        first_wave = await send_jobs(client=client, jobs=non_duplicates, concurrency=concurrency)
        second_wave = await send_jobs(client=client, jobs=duplicates, concurrency=concurrency)

    sent = first_wave + second_wave
    good_accepted = [
        event
        for event in sent
        if event.intended_fate == "good"
        and event.status_code == 200
        and event.response_payload is not None
        and event.response_payload.get("status") == "accepted"
    ]
    settled_cases = await poll_settlement(
        database_url=database_url,
        run_tag=run_tag,
        expected_good_events={(event.provider, event.event_id) for event in good_accepted},
        timeout_seconds=settle_timeout_seconds,
        poll_seconds=settle_poll_seconds,
    )
    duplicate_case_counts = query_duplicate_case_counts(
        database_url=database_url,
        duplicate_events={(event.provider, event.event_id) for event in sent if event.intended_fate == "duplicate"},
    )
    report = RunReport(
        run_tag=run_tag,
        sent=sent,
        settled_cases=settled_cases,
        duplicate_case_counts=duplicate_case_counts,
    )
    validate_report(
        report,
        classifier_mismatch_threshold=classifier_mismatch_threshold,
        require_mixed_coverage=require_mixed_coverage,
    )
    return report


async def send_jobs(
    *,
    client: httpx.AsyncClient,
    jobs: list[RequestJob],
    concurrency: int,
) -> list[SentEvent]:
    if not jobs:
        return []
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def send(job: RequestJob) -> SentEvent:
        async with semaphore:
            try:
                response = await client.post(job.endpoint, content=job.body, headers=job.headers)
                text = response.text
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                return SentEvent(
                    event_id=job.event_id,
                    scenario_id=job.scenario_id,
                    provider=job.provider,
                    intended_fate=job.intended_fate,
                    expected_exception_types=job.expected_exception_types,
                    shop_index=job.shop_index,
                    status_code=response.status_code,
                    response_payload=payload if isinstance(payload, dict) else None,
                    response_text=text,
                    duplicate_of=job.duplicate_of,
                )
            except httpx.HTTPError as exc:
                return SentEvent(
                    event_id=job.event_id,
                    scenario_id=job.scenario_id,
                    provider=job.provider,
                    intended_fate=job.intended_fate,
                    expected_exception_types=job.expected_exception_types,
                    shop_index=job.shop_index,
                    status_code=0,
                    response_payload=None,
                    response_text=str(exc),
                    duplicate_of=job.duplicate_of,
                )

    return await asyncio.gather(*(send(job) for job in jobs))


async def poll_settlement(
    *,
    database_url: str,
    run_tag: str,
    expected_good_events: set[tuple[str, str]],
    timeout_seconds: float,
    poll_seconds: float,
) -> list[SettledCase]:
    deadline = time.monotonic() + timeout_seconds
    last: list[SettledCase] = []
    while True:
        last = query_run_cases(database_url=database_url, run_tag=run_tag)
        settled_keys = {
            (case.provider, case.event_id)
            for case in last
            if case.synced or case.case_status != "open"
        }
        if expected_good_events.issubset(settled_keys):
            return last
        if time.monotonic() >= deadline:
            return last
        await asyncio.sleep(poll_seconds)


def query_run_cases(*, database_url: str, run_tag: str) -> list[SettledCase]:
    rows: list[SettledCase] = []
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        for merchant in all_merchants(connection):
            with connection.cursor() as cursor:
                set_scope(cursor, merchant["id"])
                cursor.execute(
                    """
                    select
                      we.provider,
                      we.event_id,
                      we.merchant_id::text as merchant_id,
                      we.payload,
                      c.id::text as case_id,
                      c.type as case_type,
                      c.status as case_status,
                      coalesce(c.resolution, '{}'::jsonb) as resolution,
                      exists (
                        select 1
                        from case_events ce
                        where ce.case_id = c.id
                          and ce.kind = 'agent.run_state_synced'
                      ) as synced
                    from webhook_events we
                    join cases c
                      on c.merchant_id = we.merchant_id
                     and c.subject_ref->>'provider' = we.provider
                     and c.subject_ref->>'event_id' = we.event_id
                    where we.merchant_id = %s::uuid
                      and we.payload->'synthetic'->>'run_tag' = %s
                    """,
                    (merchant["id"], run_tag),
                )
                for row in cursor.fetchall():
                    payload = json_object(row["payload"])
                    synthetic = json_object(payload.get("synthetic"))
                    expected = frozenset(
                        item
                        for item in synthetic.get("expected_exception_types", [])
                        if isinstance(item, str)
                    )
                    rows.append(
                        SettledCase(
                            event_id=str(row["event_id"]),
                            provider=str(row["provider"]),
                            merchant_id=str(row["merchant_id"]),
                            case_id=str(row["case_id"]),
                            case_type=str(row["case_type"]),
                            case_status=str(row["case_status"]),
                            scenario_id=str(synthetic.get("scenario_id") or ""),
                            expected_exception_types=expected,
                            synthetic=synthetic,
                            resolution=json_object(row["resolution"]),
                            synced=bool(row["synced"]),
                        )
                    )
    return rows


def query_duplicate_case_counts(
    *,
    database_url: str,
    duplicate_events: set[tuple[str, str]],
) -> dict[str, int]:
    if not duplicate_events:
        return {}
    counts: dict[str, int] = {}
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        for merchant in all_merchants(connection):
            with connection.cursor() as cursor:
                set_scope(cursor, merchant["id"])
                for provider, event_id in duplicate_events:
                    cursor.execute(
                        """
                        select count(*) as count
                        from cases
                        where merchant_id = %s::uuid
                          and subject_ref->>'provider' = %s
                          and subject_ref->>'event_id' = %s
                        """,
                        (merchant["id"], provider, event_id),
                    )
                    count = int(cursor.fetchone()["count"])
                    if count:
                        counts[f"{provider}:{event_id}"] = counts.get(f"{provider}:{event_id}", 0) + count
    return counts


def validate_report(
    report: RunReport,
    *,
    classifier_mismatch_threshold: float,
    require_mixed_coverage: bool,
) -> None:
    failures = report.failures
    for event in report.sent:
        if event.status_code >= 500:
            failures.append(f"{event.provider}:{event.event_id} returned unexpected {event.status_code}.")
        if event.intended_fate == "good":
            if event.status_code != 200 or event.response_payload is None:
                failures.append(f"{event.provider}:{event.event_id} did not return a JSON 200 response.")
            elif event.response_payload.get("status") != "accepted":
                failures.append(f"{event.provider}:{event.event_id} expected accepted, got {event.response_payload}.")
        elif event.intended_fate == "duplicate":
            if event.status_code != 200 or event.response_payload is None:
                failures.append(f"{event.provider}:{event.event_id} duplicate did not return JSON 200.")
            elif event.response_payload.get("status") != "duplicate":
                failures.append(f"{event.provider}:{event.event_id} expected duplicate, got {event.response_payload}.")
        elif event.intended_fate == "bad_signature" and event.status_code != 401:
            failures.append(f"{event.provider}:{event.event_id} expected 401 for bad signature, got {event.status_code}.")
        elif event.intended_fate == "malformed" and event.status_code != 400:
            failures.append(f"{event.provider}:{event.event_id} expected 400 for malformed payload, got {event.status_code}.")

    good_accepted = {
        (event.provider, event.event_id): event
        for event in report.sent
        if event.intended_fate == "good"
        and event.status_code == 200
        and event.response_payload is not None
        and event.response_payload.get("status") == "accepted"
    }
    settled_keys = {
        (case.provider, case.event_id)
        for case in report.settled_cases
        if case.synced or case.case_status != "open"
    }
    missing_settlement = sorted(good_accepted.keys() - settled_keys)
    for provider, event_id in missing_settlement:
        failures.append(f"{provider}:{event_id} did not settle before timeout.")

    for duplicate_key, count in report.duplicate_case_counts.items():
        if count != 1:
            failures.append(f"{duplicate_key} created {count} cases; expected exactly one.")

    mismatch_rate = report.classifier_mismatch_rate
    if mismatch_rate > classifier_mismatch_threshold:
        failures.append(
            f"classifier mismatch rate {mismatch_rate:.2%} exceeds "
            f"{classifier_mismatch_threshold:.2%}."
        )
    if require_mixed_coverage and report.missing_expected_coverage:
        failures.append(
            "mixed coverage is missing: " + ", ".join(report.missing_expected_coverage)
        )


def emit_report(report: RunReport, *, json_output: bool) -> None:
    status_distribution = Counter(event.status_code for event in report.sent)
    cases_by_type = Counter(case.case_type for case in report.settled_cases)
    cases_by_status = Counter(case.case_status for case in report.settled_cases)
    confusion = classifier_confusion_matrix(report.settled_cases)
    payload = {
        "run_tag": report.run_tag,
        "sent": len(report.sent),
        "accepted": report.accepted,
        "duplicates": report.duplicates,
        "bad_signature_rejections": report.bad_signature_rejections,
        "malformed_rejections": report.malformed_rejections,
        "status_code_distribution": dict(sorted(status_distribution.items())),
        "cases_found": len(report.settled_cases),
        "cases_by_type": dict(sorted(cases_by_type.items())),
        "cases_by_status": dict(sorted(cases_by_status.items())),
        "classifier_confusion_matrix": confusion,
        "missing_expected_coverage": report.missing_expected_coverage,
        "classifier_mismatch_rate": report.classifier_mismatch_rate,
        "failures": report.failures,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    print(f"run_tag: {report.run_tag}")
    print(f"sent: {payload['sent']}")
    print(f"accepted: {payload['accepted']}")
    print(f"duplicates: {payload['duplicates']}")
    print(f"bad_signature_rejections: {payload['bad_signature_rejections']}")
    print(f"malformed_rejections: {payload['malformed_rejections']}")
    print(f"status_code_distribution: {payload['status_code_distribution']}")
    print(f"cases_found: {payload['cases_found']}")
    print(f"cases_by_type: {payload['cases_by_type']}")
    print(f"cases_by_status: {payload['cases_by_status']}")
    print(f"classifier_mismatch_rate: {report.classifier_mismatch_rate:.2%}")
    print(f"missing_expected_coverage: {payload['missing_expected_coverage']}")
    print(f"classifier_confusion_matrix: {json.dumps(confusion, sort_keys=True)}")
    if report.failures:
        print("failures:")
        for failure in report.failures:
            print(f"- {failure}")
    else:
        print("result: PASS")


def classifier_confusion_matrix(cases: list[SettledCase]) -> dict[str, dict[str, int]]:
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    for case in cases:
        if not case.expected_exception_types:
            continue
        expected_label = (
            case.case_type
            if case.case_type in case.expected_exception_types
            else "|".join(sorted(case.expected_exception_types))
        )
        matrix[expected_label][case.case_type] += 1
    return {key: dict(sorted(value.items())) for key, value in sorted(matrix.items())}


def all_merchants(connection: psycopg.Connection[dict[str, Any]]) -> list[JsonObject]:
    with connection.cursor() as cursor:
        cursor.execute("select id::text as id, clerk_org_id, name from merchants order by created_at, id")
        return [dict(row) for row in cursor.fetchall()]


def set_scope(cursor: psycopg.Cursor[dict[str, Any]], merchant_id: object) -> None:
    cursor.execute("select set_config('app.merchant_id', %s, true)", (str(merchant_id),))


def root_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    try:
        contents = env_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def encode_body(payload: JsonObject) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def malformed_body(*, run_tag: str, event_id: str, scenario_id: str) -> bytes:
    return (
        b'{"synthetic":{"run_tag":"'
        + run_tag.encode("utf-8")
        + b'","scenario_id":"'
        + scenario_id.encode("utf-8")
        + b'"},"event_id":"'
        + event_id.encode("utf-8")
        + b'"'
    )


def shopify_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def stripe_signature(secret: str, body: bytes) -> str:
    timestamp = int(time.time())
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def hex_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def poison_signature(headers: dict[str, str], provider: str) -> None:
    if provider == "shopify":
        headers["X-Shopify-Hmac-Sha256"] = "bad"
    elif provider == "stripe":
        headers["Stripe-Signature"] = "t=1,v1=bad"
    elif provider == "gorgias":
        headers["X-Gorgias-Hmac-Sha256"] = "bad"


def default_run_tag() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"sim-{stamp}-{uuid4().hex[:8]}"


def short_run_hash(run_tag: str) -> str:
    return hashlib.sha1(run_tag.encode("utf-8")).hexdigest()[:8]


def json_object(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


if __name__ == "__main__":
    main()

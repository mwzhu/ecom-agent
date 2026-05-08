from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from api.config import Settings, get_settings
from api.db.models import ActorType, NormalizedEventSourceType
from api.integrations import IntegrationProvider, IntegrationRepository
from api.integrations.dependencies import get_integration_repository
from api.webhooks.classifier import (
    build_webhook_case_seed,
    is_flowlabs_demo_payload,
    webhook_external_account_id,
)
from api.webhooks.dispatcher import WebhookDispatch, WebhookDispatcher, get_webhook_dispatcher
from api.webhooks.enrichment import enrich_webhook_payload
from api.webhooks.security import verify_provider_signature

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


class WebhookAcceptedResponse(BaseModel):
    provider: IntegrationProvider
    event_id: str
    status: str
    case_id: str | None = None
    run_id: str | None = None


@router.post("/shopify", response_model=WebhookAcceptedResponse)
async def shopify_webhook(
    request: Request,
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    dispatcher: Annotated[WebhookDispatcher, Depends(get_webhook_dispatcher)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAcceptedResponse:
    return await _receive_webhook(
        IntegrationProvider.SHOPIFY,
        request=request,
        repository=repository,
        dispatcher=dispatcher,
        settings=settings,
    )


@router.post("/stripe", response_model=WebhookAcceptedResponse)
async def stripe_webhook(
    request: Request,
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    dispatcher: Annotated[WebhookDispatcher, Depends(get_webhook_dispatcher)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAcceptedResponse:
    return await _receive_webhook(
        IntegrationProvider.STRIPE,
        request=request,
        repository=repository,
        dispatcher=dispatcher,
        settings=settings,
    )


@router.post("/gorgias", response_model=WebhookAcceptedResponse)
async def gorgias_webhook(
    request: Request,
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    dispatcher: Annotated[WebhookDispatcher, Depends(get_webhook_dispatcher)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAcceptedResponse:
    return await _receive_webhook(
        IntegrationProvider.GORGIAS,
        request=request,
        repository=repository,
        dispatcher=dispatcher,
        settings=settings,
    )


@router.post("/shipbob", response_model=WebhookAcceptedResponse)
async def shipbob_webhook(
    request: Request,
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    dispatcher: Annotated[WebhookDispatcher, Depends(get_webhook_dispatcher)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAcceptedResponse:
    return await _receive_webhook(
        IntegrationProvider.SHIPBOB,
        request=request,
        repository=repository,
        dispatcher=dispatcher,
        settings=settings,
    )


@router.post("/shipstation", response_model=WebhookAcceptedResponse)
async def shipstation_webhook(
    request: Request,
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    dispatcher: Annotated[WebhookDispatcher, Depends(get_webhook_dispatcher)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAcceptedResponse:
    return await _receive_webhook(
        IntegrationProvider.SHIPSTATION,
        request=request,
        repository=repository,
        dispatcher=dispatcher,
        settings=settings,
    )


@router.post("/gmail", response_model=WebhookAcceptedResponse)
async def gmail_webhook(
    request: Request,
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    dispatcher: Annotated[WebhookDispatcher, Depends(get_webhook_dispatcher)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAcceptedResponse:
    return await _receive_webhook(
        IntegrationProvider.GMAIL,
        request=request,
        repository=repository,
        dispatcher=dispatcher,
        settings=settings,
    )


async def _receive_webhook(
    provider: IntegrationProvider,
    *,
    request: Request,
    repository: IntegrationRepository,
    dispatcher: WebhookDispatcher,
    settings: Settings,
) -> WebhookAcceptedResponse:
    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    verify_provider_signature(provider, settings=settings, body=body, headers=headers)
    payload = _parse_payload(body)
    event_id = _event_id(provider, headers, payload)
    merchant_id = await _resolve_merchant_id(
        provider,
        headers=headers,
        payload=payload,
        repository=repository,
        settings=settings,
    )
    if (
        provider is IntegrationProvider.SHOPIFY
        and headers.get("x-shopify-topic") == "orders/updated"
        and is_flowlabs_demo_payload(payload)
    ):
        return WebhookAcceptedResponse(provider=provider, event_id=event_id, status="ignored")

    await repository.set_merchant_scope(merchant_id)
    payload = await enrich_webhook_payload(
        merchant_id=merchant_id,
        provider=provider,
        payload=payload,
        repository=repository,
    )
    case_seed = build_webhook_case_seed(
        provider,
        event_id=event_id,
        headers=headers,
        payload=payload,
    )
    created = await repository.record_webhook_event(
        merchant_id=merchant_id,
        provider=provider,
        event_id=event_id,
        payload=payload,
    )
    if not created:
        return WebhookAcceptedResponse(provider=provider, event_id=event_id, status="duplicate")
    dedupe_key = f"webhook:{provider.value}:{event_id}"
    normalized_created = await repository.record_normalized_event(
        merchant_id=merchant_id,
        source_type=NormalizedEventSourceType.WEBHOOK,
        provider=provider,
        source_event_id=event_id,
        event_type=_event_type(provider, headers, payload),
        payload=payload,
        dedupe_key=dedupe_key,
    )
    if not normalized_created:
        return WebhookAcceptedResponse(provider=provider, event_id=event_id, status="duplicate")

    existing_case_id = await _existing_case_for_followup_event(
        provider=provider,
        headers=headers,
        payload=payload,
        case_seed=case_seed,
        merchant_id=merchant_id,
        repository=repository,
    )
    if existing_case_id is not None:
        await repository.record_case_event(
            merchant_id=merchant_id,
            case_id=existing_case_id,
            kind="webhook.followup_received",
            payload={
                "source_type": NormalizedEventSourceType.WEBHOOK.value,
                "provider": provider.value,
                "event_id": event_id,
                "event_type": _event_type(provider, headers, payload),
                "subject_ref": case_seed.subject_ref,
            },
            actor=ActorType.WEBHOOK,
        )
        await repository.mark_webhook_processed(provider=provider, event_id=event_id)
        await repository.mark_normalized_event_processed(
            merchant_id=merchant_id,
            dedupe_key=dedupe_key,
            case_id=existing_case_id,
        )
        return WebhookAcceptedResponse(
            provider=provider,
            event_id=event_id,
            status="updated_existing_case",
            case_id=str(existing_case_id),
        )

    thread_id = await dispatcher.create_thread()
    case_id = await repository.create_case_for_webhook(
        merchant_id=merchant_id,
        case_type=case_seed.exception_type,
        subject_ref=case_seed.subject_ref,
        langgraph_thread_id=thread_id,
    )
    await repository.record_case_event(
        merchant_id=merchant_id,
        case_id=case_id,
        kind="webhook.received",
        payload={
            "source_type": NormalizedEventSourceType.WEBHOOK.value,
            "provider": provider.value,
            "event_id": event_id,
            "exception_type": case_seed.exception_type,
            "subject_ref": case_seed.subject_ref,
            "synthetic": (
                payload.get("synthetic")
                if isinstance(payload.get("synthetic"), dict)
                else None
            ),
        },
        actor=ActorType.WEBHOOK,
    )
    run_id = await dispatcher.trigger(
        WebhookDispatch(
            provider=provider,
            event_id=event_id,
            merchant_id=merchant_id,
            case_id=case_id,
            langgraph_thread_id=thread_id,
            exception_type=case_seed.exception_type,
            order=case_seed.order,
            context=case_seed.context,
            payload=payload,
        )
    )
    await repository.record_case_event(
        merchant_id=merchant_id,
        case_id=case_id,
        kind="agent.run_started",
        payload={
            "provider": provider.value,
            "event_id": event_id,
            "thread_id": thread_id,
            "run_id": run_id,
        },
        actor=ActorType.SYSTEM,
        langsmith_run_id=run_id,
    )
    await repository.mark_webhook_processed(provider=provider, event_id=event_id)
    await repository.mark_normalized_event_processed(
        merchant_id=merchant_id,
        dedupe_key=dedupe_key,
        case_id=case_id,
    )
    return WebhookAcceptedResponse(
        provider=provider,
        event_id=event_id,
        status="accepted",
        case_id=str(case_id),
        run_id=run_id,
    )


def _parse_payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be a JSON object.",
        )
    return payload


async def _resolve_merchant_id(
    provider: IntegrationProvider,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    repository: IntegrationRepository,
    settings: Settings,
) -> UUID:
    external_account_id = webhook_external_account_id(provider, headers=headers, payload=payload)
    if external_account_id is None and provider is IntegrationProvider.STRIPE:
        external_account_id = (
            settings.stripe_account_id.lower() if settings.stripe_account_id else None
        )
    if external_account_id is not None:
        merchant_id = await repository.resolve_webhook_merchant(
            provider=provider,
            external_account_id=external_account_id,
        )
        if merchant_id is not None:
            return merchant_id

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Webhook source is not mapped to a merchant integration.",
    )


def _event_id(
    provider: IntegrationProvider,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> str:
    header_keys = {
        IntegrationProvider.SHOPIFY: "x-shopify-webhook-id",
        IntegrationProvider.GORGIAS: "x-gorgias-event-id",
        IntegrationProvider.SHIPBOB: "x-shipbob-event-id",
        IntegrationProvider.SHIPSTATION: "x-shipstation-event-id",
        IntegrationProvider.GMAIL: "x-goog-message-number",
    }
    header_value = headers.get(header_keys.get(provider, ""))
    if header_value:
        return header_value
    for key in ("id", "event_id", "webhook_id", "historyId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int):
            return str(value)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Webhook event id is missing.",
    )


def _event_type(
    provider: IntegrationProvider,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> str:
    header_keys = {
        IntegrationProvider.SHOPIFY: "x-shopify-topic",
        IntegrationProvider.GORGIAS: "x-gorgias-event",
        IntegrationProvider.SHIPBOB: "x-shipbob-event",
        IntegrationProvider.SHIPSTATION: "x-shipstation-event",
        IntegrationProvider.GMAIL: "x-goog-resource-state",
    }
    header_value = headers.get(header_keys.get(provider, ""))
    if header_value:
        return header_value
    for key in ("type", "topic", "event", "event_type"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


async def _existing_case_for_followup_event(
    *,
    provider: IntegrationProvider,
    headers: dict[str, str],
    payload: dict[str, Any],
    case_seed: Any,
    merchant_id: UUID,
    repository: IntegrationRepository,
) -> UUID | None:
    if provider is not IntegrationProvider.SHOPIFY:
        return None
    topic = headers.get("x-shopify-topic") or payload.get("topic")
    if topic not in {
        "orders/updated",
        "orders/cancelled",
        "refunds/create",
        "fulfillment_events/create",
    }:
        return None
    order_id = _provider_order_id_for_followup(payload, case_seed.subject_ref)
    if order_id is None:
        return None
    return await repository.find_case_for_provider_order(
        merchant_id=merchant_id,
        provider=provider,
        order_id=order_id,
    )


def _provider_order_id_for_followup(
    payload: dict[str, Any],
    subject_ref: dict[str, Any],
) -> str | None:
    value = payload.get("order_id") or subject_ref.get("order_id")
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int):
        return str(value)
    return None

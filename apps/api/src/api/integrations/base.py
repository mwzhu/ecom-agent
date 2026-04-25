from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import httpx
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings, get_settings
from api.db.models import (
    ActorType,
    Case,
    CaseEvent,
    CaseStatus,
    IntegrationCredential,
    ToolCall,
    ToolCallStatus,
    WebhookEvent,
    WebhookSource,
)
from api.db.session import get_sessionmaker
from api.security import CredentialCipher

JsonObject = dict[str, Any]
JsonValue = JsonObject | list[Any] | str | int | float | bool | None
Operation = Callable[["ProviderCredential"], Awaitable[JsonValue]]


class IntegrationProvider(StrEnum):
    SHOPIFY = "shopify"
    STRIPE = "stripe"
    GORGIAS = "gorgias"
    SHIPBOB = "shipbob"
    SHIPSTATION = "shipstation"
    GMAIL = "gmail"


WEBHOOK_IDENTITY_METADATA_KEYS: dict[IntegrationProvider, tuple[str, ...]] = {
    IntegrationProvider.SHOPIFY: ("shop_domain", "shop"),
    IntegrationProvider.STRIPE: ("stripe_account_id", "account_id", "account"),
    IntegrationProvider.GORGIAS: ("gorgias_domain", "domain", "account_id"),
    IntegrationProvider.SHIPBOB: ("shipbob_merchant_id", "merchant_id", "account_id"),
    IntegrationProvider.SHIPSTATION: ("shipstation_account_id", "account_id", "store_id"),
    IntegrationProvider.GMAIL: ("gmail_address", "email", "history_address"),
}


class IntegrationErrorKind(StrEnum):
    RETRYABLE = "RETRYABLE"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    FATAL = "FATAL"


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: UUID
    case_id: UUID
    idempotency_key: str | None = Field(default=None, max_length=255)


class WriteToolRequest(ToolRequest):
    idempotency_key: str = Field(min_length=8, max_length=255)


class NormalizedIntegrationError(BaseModel):
    kind: IntegrationErrorKind
    provider: IntegrationProvider
    message: str
    status_code: int | None = None
    retry_after: str | None = None
    details: JsonObject = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    provider: IntegrationProvider
    tool: str
    idempotency_key: str
    status: ToolResultStatus
    data: JsonValue = None
    error: NormalizedIntegrationError | None = None


@dataclass(frozen=True)
class ProviderCredential:
    provider: IntegrationProvider
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    metadata: Mapping[str, JsonValue]


@dataclass(frozen=True)
class ToolCallSnapshot:
    id: UUID
    status: str
    output: JsonObject | None


class IntegrationError(Exception):
    def __init__(
        self,
        kind: IntegrationErrorKind,
        provider: IntegrationProvider,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
        details: JsonObject | None = None,
    ) -> None:
        super().__init__(message)
        self.normalized = NormalizedIntegrationError(
            kind=kind,
            provider=provider,
            message=message,
            status_code=status_code,
            retry_after=retry_after,
            details=details or {},
        )


class IntegrationRepository(Protocol):
    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        ...

    async def get_credential(
        self,
        merchant_id: UUID,
        provider: IntegrationProvider,
    ) -> ProviderCredential:
        ...

    async def upsert_credential(
        self,
        merchant_id: UUID,
        provider: IntegrationProvider,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> None:
        ...

    async def get_tool_call(
        self,
        merchant_id: UUID,
        idempotency_key: str,
    ) -> ToolCallSnapshot | None:
        ...

    async def create_tool_call(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        tool: str,
        input_payload: JsonObject,
        idempotency_key: str,
    ) -> ToolCallSnapshot:
        ...

    async def finish_tool_call(
        self,
        tool_call_id: UUID,
        *,
        status: ToolCallStatus,
        output: JsonObject,
    ) -> None:
        ...

    async def record_case_event(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        kind: str,
        payload: JsonObject,
        actor: ActorType = ActorType.AGENT,
        langsmith_run_id: str | None = None,
    ) -> None:
        ...

    async def resolve_webhook_merchant(
        self,
        *,
        provider: IntegrationProvider,
        external_account_id: str,
    ) -> UUID | None:
        ...

    async def create_case_for_webhook(
        self,
        *,
        merchant_id: UUID,
        case_type: str,
        subject_ref: JsonObject,
        langgraph_thread_id: str,
    ) -> UUID:
        ...

    async def record_webhook_event(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        event_id: str,
        payload: JsonObject,
    ) -> bool:
        ...

    async def mark_webhook_processed(
        self,
        *,
        provider: IntegrationProvider,
        event_id: str,
    ) -> None:
        ...


class SqlAlchemyIntegrationRepository:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()

    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        await self._session.execute(
            text("select set_config('app.merchant_id', :merchant_id, true)"),
            {"merchant_id": str(merchant_id)},
        )

    async def get_credential(
        self,
        merchant_id: UUID,
        provider: IntegrationProvider,
    ) -> ProviderCredential:
        result = await self._session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.merchant_id == merchant_id,
                IntegrationCredential.provider == provider.value,
            )
        )
        credential = result.scalar_one_or_none()
        if credential is None:
            raise IntegrationError(
                IntegrationErrorKind.AUTH_EXPIRED,
                provider,
                f"No active {provider.value} credential is stored for this merchant.",
            )

        cipher = _credential_cipher(self._settings)
        raw_access_token = cipher.decrypt(merchant_id, credential.encrypted_token)
        access_token, metadata = _decode_token_payload(raw_access_token)
        refresh_token = (
            cipher.decrypt(merchant_id, credential.encrypted_refresh)
            if credential.encrypted_refresh is not None
            else None
        )
        return ProviderCredential(
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=credential.expires_at,
            metadata=metadata,
        )

    async def upsert_credential(
        self,
        merchant_id: UUID,
        provider: IntegrationProvider,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> None:
        cipher = _credential_cipher(self._settings)
        encrypted_token = cipher.encrypt(
            merchant_id,
            _encode_token_payload(access_token, metadata or {}),
        )
        encrypted_refresh = (
            cipher.encrypt(merchant_id, refresh_token).encrypted_value
            if refresh_token is not None
            else None
        )
        values = {
            "merchant_id": merchant_id,
            "provider": provider.value,
            "encrypted_token": encrypted_token.encrypted_value,
            "encrypted_refresh": encrypted_refresh,
            "expires_at": expires_at,
            "kms_key_id": encrypted_token.kms_key_id,
        }
        statement = postgres_insert(IntegrationCredential).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_integration_credentials_provider",
            set_={
                "encrypted_token": statement.excluded.encrypted_token,
                "encrypted_refresh": statement.excluded.encrypted_refresh,
                "expires_at": statement.excluded.expires_at,
                "kms_key_id": statement.excluded.kms_key_id,
            },
        )
        await self._session.execute(statement)
        external_account_id = webhook_external_account_id(provider, metadata or {})
        if external_account_id is not None:
            source_statement = postgres_insert(WebhookSource).values(
                merchant_id=merchant_id,
                provider=provider.value,
                external_account_id=external_account_id,
            )
            source_statement = source_statement.on_conflict_do_update(
                constraint="uq_webhook_sources_provider_external",
                set_={"merchant_id": source_statement.excluded.merchant_id},
            )
            await self._session.execute(source_statement)

    async def get_tool_call(
        self,
        merchant_id: UUID,
        idempotency_key: str,
    ) -> ToolCallSnapshot | None:
        result = await self._session.execute(
            select(ToolCall).where(
                ToolCall.merchant_id == merchant_id,
                ToolCall.idempotency_key == idempotency_key,
            )
        )
        tool_call = result.scalar_one_or_none()
        if tool_call is None:
            return None
        return ToolCallSnapshot(
            id=tool_call.id,
            status=tool_call.status,
            output=tool_call.output,
        )

    async def create_tool_call(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        tool: str,
        input_payload: JsonObject,
        idempotency_key: str,
    ) -> ToolCallSnapshot:
        tool_call = ToolCall(
            merchant_id=merchant_id,
            case_id=case_id,
            tool=tool,
            input=input_payload,
            idempotency_key=idempotency_key,
            status=ToolCallStatus.PENDING,
        )
        self._session.add(tool_call)
        await self._session.flush()
        return ToolCallSnapshot(id=tool_call.id, status=tool_call.status, output=tool_call.output)

    async def finish_tool_call(
        self,
        tool_call_id: UUID,
        *,
        status: ToolCallStatus,
        output: JsonObject,
    ) -> None:
        await self._session.execute(
            update(ToolCall)
            .where(ToolCall.id == tool_call_id)
            .values(status=status.value, output=output)
        )

    async def record_case_event(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        kind: str,
        payload: JsonObject,
        actor: ActorType = ActorType.AGENT,
        langsmith_run_id: str | None = None,
    ) -> None:
        self._session.add(
            CaseEvent(
                merchant_id=merchant_id,
                case_id=case_id,
                kind=kind,
                payload=payload,
                actor=actor.value,
                langsmith_run_id=langsmith_run_id,
            )
        )
        await self._session.flush()

    async def resolve_webhook_merchant(
        self,
        *,
        provider: IntegrationProvider,
        external_account_id: str,
    ) -> UUID | None:
        result = await self._session.execute(
            select(WebhookSource).where(
                WebhookSource.provider == provider.value,
                WebhookSource.external_account_id == external_account_id,
            )
        )
        source = result.scalar_one_or_none()
        return source.merchant_id if source is not None else None

    async def create_case_for_webhook(
        self,
        *,
        merchant_id: UUID,
        case_type: str,
        subject_ref: JsonObject,
        langgraph_thread_id: str,
    ) -> UUID:
        case = Case(
            merchant_id=merchant_id,
            type=case_type,
            status=CaseStatus.OPEN.value,
            subject_ref=subject_ref,
            langgraph_thread_id=langgraph_thread_id,
        )
        self._session.add(case)
        await self._session.flush()
        return case.id

    async def record_webhook_event(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        event_id: str,
        payload: JsonObject,
    ) -> bool:
        statement = (
            postgres_insert(WebhookEvent)
            .values(
                merchant_id=merchant_id,
                provider=provider.value,
                event_id=event_id,
                payload=payload,
            )
            .on_conflict_do_nothing(constraint="uq_webhook_events_provider_event")
            .returning(WebhookEvent.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def mark_webhook_processed(
        self,
        *,
        provider: IntegrationProvider,
        event_id: str,
    ) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.provider == provider.value, WebhookEvent.event_id == event_id)
            .values(processed_at=datetime.now(UTC))
        )


async def run_tool_with_session(
    *,
    provider: IntegrationProvider,
    tool_name: str,
    request: ToolRequest,
    operation: Operation,
    write: bool = False,
) -> JsonObject:
    settings = get_settings()
    sessionmaker = get_sessionmaker(settings)
    async with sessionmaker() as session:
        async with session.begin():
            repository = SqlAlchemyIntegrationRepository(session, settings)
            result = await execute_integration_tool(
                repository=repository,
                provider=provider,
                tool_name=tool_name,
                request=request,
                operation=operation,
                write=write,
            )
            return result.model_dump(mode="json")


async def execute_integration_tool(
    *,
    repository: IntegrationRepository,
    provider: IntegrationProvider,
    tool_name: str,
    request: ToolRequest,
    operation: Operation,
    write: bool = False,
) -> ToolExecutionResult:
    await repository.set_merchant_scope(request.merchant_id)
    idempotency_key = request.idempotency_key or build_idempotency_key(tool_name, request)
    if write and request.idempotency_key is None:
        error = NormalizedIntegrationError(
            kind=IntegrationErrorKind.FATAL,
            provider=provider,
            message="Write tools require an explicit idempotency_key.",
        )
        return ToolExecutionResult(
            provider=provider,
            tool=tool_name,
            idempotency_key=idempotency_key,
            status=ToolResultStatus.FAILED,
            error=error,
        )

    existing = await repository.get_tool_call(request.merchant_id, idempotency_key)
    if existing is not None:
        return _result_from_existing(provider, tool_name, idempotency_key, existing)

    input_payload = _request_payload(request)
    tool_call = await repository.create_tool_call(
        merchant_id=request.merchant_id,
        case_id=request.case_id,
        tool=tool_name,
        input_payload=input_payload,
        idempotency_key=idempotency_key,
    )
    try:
        credential = await repository.get_credential(request.merchant_id, provider)
        data = jsonable_encoder(await operation(credential))
    except Exception as exc:  # noqa: BLE001 - every tool error must be normalized for agents.
        error = normalize_exception(provider, exc)
        output = {
            "provider": provider.value,
            "tool": tool_name,
            "error": error.model_dump(mode="json"),
        }
        await repository.finish_tool_call(
            tool_call.id,
            status=ToolCallStatus.FAILED,
            output=output,
        )
        await repository.record_case_event(
            merchant_id=request.merchant_id,
            case_id=request.case_id,
            kind="tool_call.failed",
            payload={
                "tool": tool_name,
                "idempotency_key": idempotency_key,
                "error": output["error"],
            },
        )
        return ToolExecutionResult(
            provider=provider,
            tool=tool_name,
            idempotency_key=idempotency_key,
            status=ToolResultStatus.FAILED,
            error=error,
        )

    output = {"provider": provider.value, "tool": tool_name, "data": data}
    await repository.finish_tool_call(
        tool_call.id,
        status=ToolCallStatus.SUCCEEDED,
        output=output,
    )
    await repository.record_case_event(
        merchant_id=request.merchant_id,
        case_id=request.case_id,
        kind="tool_call.succeeded",
        payload={"tool": tool_name, "idempotency_key": idempotency_key, "data": data},
    )
    return ToolExecutionResult(
        provider=provider,
        tool=tool_name,
        idempotency_key=idempotency_key,
        status=ToolResultStatus.SUCCEEDED,
        data=data,
    )


def build_idempotency_key(tool_name: str, request: ToolRequest) -> str:
    payload = _request_payload(request)
    stable_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"idempotency_key", "merchant_id"}
    }
    digest = hashlib.sha256(
        json.dumps(stable_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{tool_name}:{digest}"


def normalize_exception(
    provider: IntegrationProvider,
    exc: Exception,
) -> NormalizedIntegrationError:
    if isinstance(exc, IntegrationError):
        return exc.normalized
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return _normalize_http_status(
            provider,
            status_code=response.status_code,
            message=response.text[:500] or response.reason_phrase,
            retry_after=response.headers.get("retry-after"),
        )
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
        ),
    ):
        return NormalizedIntegrationError(
            kind=IntegrationErrorKind.RETRYABLE,
            provider=provider,
            message=str(exc) or "Provider request failed before a response was received.",
        )
    return NormalizedIntegrationError(
        kind=IntegrationErrorKind.FATAL,
        provider=provider,
        message=str(exc) or exc.__class__.__name__,
    )


def _normalize_http_status(
    provider: IntegrationProvider,
    *,
    status_code: int,
    message: str,
    retry_after: str | None = None,
) -> NormalizedIntegrationError:
    if status_code in {401, 403}:
        kind = IntegrationErrorKind.AUTH_EXPIRED
    elif status_code == 429:
        kind = IntegrationErrorKind.RATE_LIMITED
    elif status_code >= 500:
        kind = IntegrationErrorKind.RETRYABLE
    else:
        kind = IntegrationErrorKind.FATAL
    return NormalizedIntegrationError(
        kind=kind,
        provider=provider,
        message=message,
        status_code=status_code,
        retry_after=retry_after,
    )


def _result_from_existing(
    provider: IntegrationProvider,
    tool_name: str,
    idempotency_key: str,
    existing: ToolCallSnapshot,
) -> ToolExecutionResult:
    output = existing.output or {}
    raw_error = output.get("error")
    error = (
        NormalizedIntegrationError.model_validate(raw_error)
        if isinstance(raw_error, dict)
        else None
    )
    return ToolExecutionResult(
        provider=provider,
        tool=tool_name,
        idempotency_key=idempotency_key,
        status=ToolResultStatus.SKIPPED,
        data=output.get("data"),
        error=error,
    )


def _request_payload(request: ToolRequest) -> JsonObject:
    payload = jsonable_encoder(request.model_dump(mode="json"))
    if not isinstance(payload, dict):
        raise TypeError("Tool request encoded to a non-object JSON payload.")
    return payload


def _credential_cipher(settings: Settings) -> CredentialCipher:
    if settings.local_kms_master_key is None:
        raise RuntimeError("LOCAL_KMS_MASTER_KEY is required to decrypt integration credentials.")
    return CredentialCipher(
        master_key=settings.local_kms_master_key,
        kms_key_id=settings.app_kms_key_id,
    )


def _encode_token_payload(access_token: str, metadata: Mapping[str, JsonValue]) -> str:
    if not metadata:
        return access_token
    return json.dumps(
        {"access_token": access_token, "metadata": metadata},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_token_payload(raw_value: str) -> tuple[str, Mapping[str, JsonValue]]:
    try:
        decoded = json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value, {}
    if not isinstance(decoded, dict):
        return raw_value, {}
    token = decoded.get("access_token") or decoded.get("token") or decoded.get("api_key")
    if not isinstance(token, str) or not token:
        return raw_value, {}
    metadata = decoded.get("metadata")
    return token, metadata if isinstance(metadata, dict) else {}


def webhook_external_account_id(
    provider: IntegrationProvider,
    metadata: Mapping[str, JsonValue],
) -> str | None:
    for key in webhook_identity_metadata_keys(provider):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value.lower()
        if isinstance(value, int):
            return str(value)
    return None


def webhook_identity_metadata_keys(provider: IntegrationProvider) -> tuple[str, ...]:
    return WEBHOOK_IDENTITY_METADATA_KEYS[provider]


def require_metadata_string(
    credential: ProviderCredential,
    key: str,
    fallback: str | None = None,
) -> str:
    value = credential.metadata.get(key)
    if isinstance(value, str) and value:
        return value
    if fallback is not None:
        return fallback
    raise IntegrationError(
        IntegrationErrorKind.FATAL,
        credential.provider,
        f"Credential metadata is missing required field {key!r}.",
    )

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

import httpx
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings, get_settings
from api.db.models import (
    ActorType,
    AuditEvent,
    Case,
    CaseEvent,
    CaseStatus,
    IntegrationCredential,
    IntegrationCredentialStatus,
    IntegrationHealthStatus,
    NormalizedEvent,
    NormalizedEventSourceType,
    ToolCall,
    ToolCallStatus,
    WebhookEvent,
    WebhookRegistrationStatus,
    WebhookRegistry,
    WebhookSource,
)
from api.db.session import get_sessionmaker
from api.security import CredentialCipher, EncryptedCredential, ManagedKmsCredentialCipher

if TYPE_CHECKING:
    from api.integrations.scopes import ToolAvailability

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
class CredentialHealthSnapshot:
    provider: IntegrationProvider
    status: str
    provider_account_id: str | None
    granted_scopes: list[str]
    missing_scopes: list[str]
    checked_at: datetime | None
    error: JsonObject | None


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

    async def list_credential_health(self, merchant_id: UUID) -> list[CredentialHealthSnapshot]:
        ...

    async def update_credential_health(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        status: IntegrationHealthStatus,
        provider_account_id: str | None,
        granted_scopes: list[str],
        error: JsonObject | None = None,
    ) -> None:
        ...

    async def disconnect_provider(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        actor_id: str | None,
        retention_mark_cached_data: bool = True,
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

    async def find_case_for_provider_order(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        order_id: str,
    ) -> UUID | None:
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

    async def record_normalized_event(
        self,
        *,
        merchant_id: UUID,
        source_type: NormalizedEventSourceType,
        provider: IntegrationProvider | None,
        source_event_id: str,
        event_type: str,
        payload: JsonObject,
        dedupe_key: str,
    ) -> bool:
        ...

    async def mark_normalized_event_processed(
        self,
        *,
        merchant_id: UUID,
        dedupe_key: str,
        case_id: UUID,
    ) -> None:
        ...

    async def upsert_webhook_registration(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        topic: str,
        callback_url: str,
        external_webhook_id: str | None,
        signing_secret_ref: str | None,
        status: WebhookRegistrationStatus,
        verified: bool,
    ) -> None:
        ...

    async def record_audit_event(
        self,
        *,
        merchant_id: UUID,
        actor: ActorType,
        action: str,
        payload: JsonObject,
        actor_id: str | None = None,
        provider: IntegrationProvider | None = None,
        case_id: UUID | None = None,
    ) -> None:
        ...

    async def mark_webhook_processed(
        self,
        *,
        provider: IntegrationProvider,
        event_id: str,
    ) -> None:
        ...


class CredentialEnvelopeCipher(Protocol):
    def encrypt(self, merchant_id: UUID, plaintext: str) -> EncryptedCredential:
        ...

    def decrypt(self, merchant_id: UUID, encrypted_value: str) -> str:
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
                IntegrationCredential.status == IntegrationCredentialStatus.ACTIVE.value,
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

    async def list_credential_health(self, merchant_id: UUID) -> list[CredentialHealthSnapshot]:
        result = await self._session.execute(
            select(IntegrationCredential)
            .where(IntegrationCredential.merchant_id == merchant_id)
            .order_by(IntegrationCredential.provider.asc())
        )
        return [
            CredentialHealthSnapshot(
                provider=IntegrationProvider(credential.provider),
                status=credential.last_health_status,
                provider_account_id=credential.provider_account_id,
                granted_scopes=_string_list(credential.granted_scopes),
                missing_scopes=_string_list(
                    (credential.last_health_error or {}).get("missing_scopes")
                    if isinstance(credential.last_health_error, dict)
                    else []
                ),
                checked_at=credential.last_health_checked_at,
                error=credential.last_health_error,
            )
            for credential in result.scalars()
        ]

    async def update_credential_health(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        status: IntegrationHealthStatus,
        provider_account_id: str | None,
        granted_scopes: list[str],
        error: JsonObject | None = None,
    ) -> None:
        await self._session.execute(
            update(IntegrationCredential)
            .where(
                IntegrationCredential.merchant_id == merchant_id,
                IntegrationCredential.provider == provider.value,
            )
            .values(
                provider_account_id=provider_account_id,
                granted_scopes=granted_scopes,
                last_health_status=status.value,
                last_health_error=error,
                last_health_checked_at=datetime.now(UTC),
            )
        )

    async def disconnect_provider(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        actor_id: str | None,
        retention_mark_cached_data: bool = True,
    ) -> None:
        now = datetime.now(UTC)
        await self._session.execute(
            update(IntegrationCredential)
            .where(
                IntegrationCredential.merchant_id == merchant_id,
                IntegrationCredential.provider == provider.value,
            )
            .values(
                status=IntegrationCredentialStatus.DISCONNECTED.value,
                encrypted_token="",
                encrypted_refresh=None,
                last_health_status=IntegrationHealthStatus.AUTH_FAILED.value,
                last_health_error={"reason": "provider_disconnected"},
                disconnected_at=now,
            )
        )
        await self._session.execute(
            update(WebhookRegistry)
            .where(
                WebhookRegistry.merchant_id == merchant_id,
                WebhookRegistry.provider == provider.value,
            )
            .values(status=WebhookRegistrationStatus.DISABLED.value, last_verified_at=now)
        )
        await self._session.execute(
            delete(WebhookSource).where(
                WebhookSource.merchant_id == merchant_id,
                WebhookSource.provider == provider.value,
            )
        )
        await self.record_audit_event(
            merchant_id=merchant_id,
            actor=ActorType.HUMAN if actor_id else ActorType.SYSTEM,
            actor_id=actor_id,
            provider=provider,
            action="integration.disconnected",
            payload={"retention_mark_cached_data": retention_mark_cached_data},
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
            "status": IntegrationCredentialStatus.ACTIVE.value,
            "provider_account_id": webhook_external_account_id(provider, metadata or {}),
            "granted_scopes": scopes_from_metadata(metadata or {}),
            "last_health_status": IntegrationHealthStatus.UNKNOWN.value,
            "last_health_error": None,
            "last_health_checked_at": None,
            "disconnected_at": None,
        }
        statement = postgres_insert(IntegrationCredential).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_integration_credentials_provider",
            set_={
                "encrypted_token": statement.excluded.encrypted_token,
                "encrypted_refresh": statement.excluded.encrypted_refresh,
                "expires_at": statement.excluded.expires_at,
                "kms_key_id": statement.excluded.kms_key_id,
                "status": statement.excluded.status,
                "provider_account_id": statement.excluded.provider_account_id,
                "granted_scopes": statement.excluded.granted_scopes,
                "last_health_status": statement.excluded.last_health_status,
                "last_health_error": statement.excluded.last_health_error,
                "last_health_checked_at": statement.excluded.last_health_checked_at,
                "disconnected_at": statement.excluded.disconnected_at,
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
        await self.record_audit_event(
            merchant_id=merchant_id,
            actor=ActorType.SYSTEM,
            action="integration.credential_upserted",
            provider=provider,
            payload={
                "provider_account_id": values["provider_account_id"],
                "granted_scopes": values["granted_scopes"],
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )

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

    async def find_case_for_provider_order(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        order_id: str,
    ) -> UUID | None:
        result = await self._session.execute(
            select(Case.id)
            .where(
                Case.merchant_id == merchant_id,
                Case.subject_ref["provider"].as_string() == provider.value,
                Case.subject_ref["order_id"].as_string() == str(order_id),
            )
            .order_by(Case.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

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

    async def record_normalized_event(
        self,
        *,
        merchant_id: UUID,
        source_type: NormalizedEventSourceType,
        provider: IntegrationProvider | None,
        source_event_id: str,
        event_type: str,
        payload: JsonObject,
        dedupe_key: str,
    ) -> bool:
        statement = (
            postgres_insert(NormalizedEvent)
            .values(
                merchant_id=merchant_id,
                source_type=source_type.value,
                provider=provider.value if provider else None,
                source_event_id=source_event_id,
                event_type=event_type,
                payload=payload,
                dedupe_key=dedupe_key,
                observed_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_normalized_events_dedupe")
            .returning(NormalizedEvent.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def mark_normalized_event_processed(
        self,
        *,
        merchant_id: UUID,
        dedupe_key: str,
        case_id: UUID,
    ) -> None:
        await self._session.execute(
            update(NormalizedEvent)
            .where(
                NormalizedEvent.merchant_id == merchant_id,
                NormalizedEvent.dedupe_key == dedupe_key,
            )
            .values(processed_at=datetime.now(UTC), case_id=case_id)
        )

    async def upsert_webhook_registration(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        topic: str,
        callback_url: str,
        external_webhook_id: str | None,
        signing_secret_ref: str | None,
        status: WebhookRegistrationStatus,
        verified: bool,
    ) -> None:
        now = datetime.now(UTC)
        statement = postgres_insert(WebhookRegistry).values(
            merchant_id=merchant_id,
            provider=provider.value,
            topic=topic,
            external_webhook_id=external_webhook_id,
            signing_secret_ref=signing_secret_ref,
            status=status.value,
            callback_url=callback_url,
            last_verified_at=now if verified else None,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_webhook_registry_merchant_provider_topic",
            set_={
                "external_webhook_id": statement.excluded.external_webhook_id,
                "signing_secret_ref": statement.excluded.signing_secret_ref,
                "status": statement.excluded.status,
                "callback_url": statement.excluded.callback_url,
                "last_verified_at": statement.excluded.last_verified_at,
            },
        )
        await self._session.execute(statement)

    async def record_audit_event(
        self,
        *,
        merchant_id: UUID,
        actor: ActorType,
        action: str,
        payload: JsonObject,
        actor_id: str | None = None,
        provider: IntegrationProvider | None = None,
        case_id: UUID | None = None,
    ) -> None:
        self._session.add(
            AuditEvent(
                merchant_id=merchant_id,
                actor_type=actor.value,
                actor_id=actor_id,
                action=action,
                provider=provider.value if provider else None,
                case_id=case_id,
                payload=payload,
            )
        )
        await self._session.flush()

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
    settings = get_settings()
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
        availability = _tool_availability(tool_name, credential, settings)
        if availability is not None and not availability.enabled:
            raise IntegrationError(
                IntegrationErrorKind.FATAL,
                provider,
                f"Tool {tool_name} is disabled for this credential.",
                details={
                    "block_reasons": availability.block_reasons,
                    "missing_scopes": availability.missing_scopes,
                },
            )
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


def _credential_cipher(settings: Settings) -> CredentialEnvelopeCipher:
    if settings.app_kms_provider == "managed":
        if settings.managed_kms_key_id is None:
            raise RuntimeError("MANAGED_KMS_KEY_ID is required for managed credential KMS.")
        return ManagedKmsCredentialCipher(settings.managed_kms_key_id)
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


def _tool_availability(
    tool_name: str,
    credential: ProviderCredential,
    settings: Settings,
) -> ToolAvailability | None:
    from api.integrations.scopes import tool_availability

    if not any(key in credential.metadata for key in ("scope", "scopes", "granted_scopes")):
        return None
    return tool_availability(
        tool_name,
        granted_scopes=scopes_from_metadata(credential.metadata),
        settings=settings,
    )


def scopes_from_metadata(metadata: Mapping[str, JsonValue]) -> list[str]:
    scope_value = metadata.get("scope") or metadata.get("scopes") or metadata.get("granted_scopes")
    if isinstance(scope_value, str):
        return sorted({scope.strip() for scope in scope_value.replace(" ", ",").split(",") if scope.strip()})
    if isinstance(scope_value, list):
        return sorted({scope for scope in scope_value if isinstance(scope, str) and scope})
    return []


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]

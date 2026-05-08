from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class CaseStatus(StrEnum):
    OPEN = "open"
    PENDING_APPROVAL = "pending_approval"
    EXECUTING = "executing"
    PARTIALLY_EXECUTED = "partially_executed"
    NEEDS_HUMAN_RECOVERY = "needs_human_recovery"
    RESOLVED = "resolved"
    FAILED = "failed"
    CANCELED = "canceled"


class FopStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvalCorrectionStatus(StrEnum):
    QUEUED = "queued"
    EXPORTED = "exported"


class EvalReviewStatus(StrEnum):
    QUEUED = "queued"
    REVIEWED = "reviewed"
    DISMISSED = "dismissed"


class AgentRunExecutionStatus(StrEnum):
    PROCESSING = "processing"
    SYNCED = "synced"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IntegrationCredentialStatus(StrEnum):
    ACTIVE = "active"
    DISCONNECTED = "disconnected"
    REVOKED = "revoked"


class IntegrationHealthStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    MISSING_SCOPES = "missing_scopes"
    AUTH_FAILED = "auth_failed"
    ERROR = "error"


class WebhookRegistrationStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class NormalizedEventSourceType(StrEnum):
    WEBHOOK = "webhook"
    SCHEDULED_MONITOR = "scheduled_monitor"
    BACKFILL = "backfill"
    MANUAL = "manual"


class ApprovalRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    EXPIRED = "expired"


class PolicyApprovalMode(StrEnum):
    AUTO_EXECUTE = "auto_execute"
    APPROVAL_REQUIRED = "approval_required"
    DRAFT_ONLY = "draft_only"
    NEVER_ACT = "never_act"


class CustomerMessageMode(StrEnum):
    DISABLED = "disabled"
    DRAFT_ONLY = "draft_only"
    AUTO_CREATE_DRAFT = "auto_create_draft"
    AUTO_SEND = "auto_send"


class ActorType(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    WEBHOOK = "webhook"
    SYSTEM = "system"


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    clerk_org_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    tier: Mapped[str] = mapped_column(String(32), default="internal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntegrationCredential(Base):
    __tablename__ = "integration_credentials"
    __table_args__ = (
        UniqueConstraint("merchant_id", "provider", name="uq_integration_credentials_provider"),
        Index("ix_integration_credentials_merchant_provider", "merchant_id", "provider"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(64))
    encrypted_token: Mapped[str] = mapped_column(Text)
    encrypted_refresh: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kms_key_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=IntegrationCredentialStatus.ACTIVE)
    provider_account_id: Mapped[str | None] = mapped_column(String(255))
    granted_scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_health_status: Mapped[str] = mapped_column(
        String(32),
        default=IntegrationHealthStatus.UNKNOWN,
    )
    last_health_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_merchant_created", "merchant_id", "created_at"),
        Index("ix_audit_events_actor_created", "actor_type", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(64))
    case_id: Mapped[UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookSource(Base):
    __tablename__ = "webhook_sources"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_account_id",
            name="uq_webhook_sources_provider_external",
        ),
        Index("ix_webhook_sources_merchant_provider", "merchant_id", "provider"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(64))
    external_account_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookRegistry(Base):
    __tablename__ = "webhook_registry"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "provider",
            "topic",
            name="uq_webhook_registry_merchant_provider_topic",
        ),
        Index("ix_webhook_registry_provider_status", "provider", "status"),
        Index("ix_webhook_registry_merchant_provider", "merchant_id", "provider"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(128))
    external_webhook_id: Mapped[str | None] = mapped_column(String(255))
    signing_secret_ref: Mapped[str | None] = mapped_column(String(255))
    previous_signing_secret_ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=WebhookRegistrationStatus.PENDING)
    callback_url: Mapped[str] = mapped_column(String(2048))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secret_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_secret_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        Index("ix_cases_merchant_status_created", "merchant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=CaseStatus.OPEN)
    subject_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    langgraph_thread_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class CaseEvent(Base):
    __tablename__ = "case_events"
    __table_args__ = (
        CheckConstraint(
            "actor in ('agent', 'human', 'webhook', 'system')",
            name="ck_case_events_actor",
        ),
        Index("ix_case_events_case_created", "case_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    langsmith_run_id: Mapped[str | None] = mapped_column(String(255))
    actor: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Fop(Base):
    __tablename__ = "fops"
    __table_args__ = (
        Index("ix_fops_merchant_status_created", "merchant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(default=1)
    nl_text: Mapped[str] = mapped_column(Text)
    structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=FopStatus.DRAFT)
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("fops.id", ondelete="SET NULL"))
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_webhook_events_provider_event"),
        Index("ix_webhook_events_merchant_processed", "merchant_id", "processed_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(255))
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NormalizedEvent(Base):
    __tablename__ = "normalized_events"
    __table_args__ = (
        UniqueConstraint("merchant_id", "dedupe_key", name="uq_normalized_events_dedupe"),
        Index(
            "ix_normalized_events_merchant_processed",
            "merchant_id",
            "processed_at",
            "observed_at",
        ),
        Index("ix_normalized_events_source_provider", "source_type", "provider"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(64))
    source_event_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(512))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    case_id: Mapped[UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduledMonitorCursor(Base):
    __tablename__ = "scheduled_monitor_cursors"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "provider",
            "monitor_type",
            name="uq_scheduled_monitor_cursors_monitor",
        ),
        Index("ix_scheduled_monitor_cursors_next_run", "next_run_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(64))
    monitor_type: Mapped[str] = mapped_column(String(128))
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cadence_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_limit_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class AutomationPolicy(Base):
    __tablename__ = "automation_policies"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "exception_type",
            "tool",
            name="uq_automation_policies_scope",
        ),
        Index("ix_automation_policies_merchant_exception", "merchant_id", "exception_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    exception_type: Mapped[str] = mapped_column(String(128))
    tool: Mapped[str] = mapped_column(String(128), default="*")
    approval_mode: Mapped[str] = mapped_column(
        String(32),
        default=PolicyApprovalMode.APPROVAL_REQUIRED,
    )
    customer_message_mode: Mapped[str] = mapped_column(
        String(32),
        default=CustomerMessageMode.DRAFT_ONLY,
    )
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(255), default="system")
    updated_by: Mapped[str] = mapped_column(String(255), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        Index("ix_approval_requests_merchant_status_due", "merchant_id", "status", "due_at"),
        Index("ix_approval_requests_case_created", "case_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    case_id: Mapped[UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    assigned_group: Mapped[str] = mapped_column(String(128))
    primary_approver: Mapped[str | None] = mapped_column(String(255))
    escalation_group: Mapped[str | None] = mapped_column(String(128))
    requested_action: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default=ApprovalRequestStatus.PENDING)
    timeout_behavior: Mapped[str] = mapped_column(String(32), default="no_auto_approve")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("merchant_id", "template_key", name="uq_message_templates_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    template_key: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(32), default="email")
    brand_voice: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GeneratedMessageAudit(Base):
    __tablename__ = "generated_message_audits"
    __table_args__ = (
        Index("ix_generated_message_audits_case_created", "case_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    case_id: Mapped[UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    provider: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(32), default="email")
    body: Mapped[str] = mapped_column(Text)
    policy_decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("merchant_id", "idempotency_key", name="uq_tool_calls_idempotency"),
        Index("ix_tool_calls_case_created", "case_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    tool: Mapped[str] = mapped_column(String(128))
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=ToolCallStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalCorrection(Base):
    __tablename__ = "eval_corrections"
    __table_args__ = (
        Index("ix_eval_corrections_merchant_created", "merchant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    expected_resolution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=EvalCorrectionStatus.QUEUED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalReviewItem(Base):
    __tablename__ = "eval_review_items"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "case_id",
            "langsmith_run_id",
            name="uq_eval_review_items_run",
        ),
        Index(
            "ix_eval_review_items_merchant_status_created",
            "merchant_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    langsmith_run_id: Mapped[str | None] = mapped_column(String(255))
    score: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=EvalReviewStatus.QUEUED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRunExecution(Base):
    __tablename__ = "agent_run_executions"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_agent_run_executions_run"),
        Index(
            "ix_agent_run_executions_merchant_status_created",
            "merchant_id",
            "status",
            "created_at",
        ),
        Index("ix_agent_run_executions_case_created", "case_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"))
    thread_id: Mapped[str] = mapped_column(String(255))
    run_id: Mapped[str] = mapped_column(String(255))
    graph_status: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=AgentRunExecutionStatus.PROCESSING)
    graph_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    execution_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

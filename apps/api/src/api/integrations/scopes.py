from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from api.config import Settings
from api.integrations.base import IntegrationProvider


class ToolBlockReason(StrEnum):
    MISSING_SCOPES = "missing_scopes"
    GLOBAL_WRITE_DISABLED = "global_write_disabled"
    PROVIDER_WRITE_DISABLED = "provider_write_disabled"
    TOOL_DISABLED = "tool_disabled"


@dataclass(frozen=True)
class ToolScopeRequirement:
    provider: IntegrationProvider
    scopes: frozenset[str]
    write: bool = False
    irreversible: bool = False


@dataclass(frozen=True)
class ToolAvailability:
    enabled: bool
    provider: IntegrationProvider
    missing_scopes: list[str]
    block_reasons: list[str]


TOOL_SCOPE_REQUIREMENTS: dict[str, ToolScopeRequirement] = {
    "shopify_get_order": ToolScopeRequirement(
        IntegrationProvider.SHOPIFY,
        frozenset({"read_orders"}),
    ),
    "shopify_search_orders": ToolScopeRequirement(
        IntegrationProvider.SHOPIFY,
        frozenset({"read_orders"}),
    ),
    "shopify_update_order_note": ToolScopeRequirement(
        IntegrationProvider.SHOPIFY,
        frozenset({"read_orders", "write_orders"}),
        write=True,
    ),
    "shopify_update_shipping_address": ToolScopeRequirement(
        IntegrationProvider.SHOPIFY,
        frozenset({"read_orders", "write_orders"}),
        write=True,
    ),
    "shopify_apply_order_edit": ToolScopeRequirement(
        IntegrationProvider.SHOPIFY,
        frozenset({"read_orders", "write_order_edits"}),
        write=True,
        irreversible=True,
    ),
    "shopify_cancel_order": ToolScopeRequirement(
        IntegrationProvider.SHOPIFY,
        frozenset({"read_orders", "write_orders"}),
        write=True,
        irreversible=True,
    ),
    "shopify_create_refund": ToolScopeRequirement(
        IntegrationProvider.SHOPIFY,
        frozenset({"read_orders", "write_orders"}),
        write=True,
        irreversible=True,
    ),
    "shopify_hold_fulfillment_order": ToolScopeRequirement(
        IntegrationProvider.SHOPIFY,
        frozenset({"read_orders", "read_fulfillments", "write_fulfillments"}),
        write=True,
    ),
    "stripe_get_charge": ToolScopeRequirement(
        IntegrationProvider.STRIPE,
        frozenset({"charges:read"}),
    ),
    "stripe_get_dispute": ToolScopeRequirement(
        IntegrationProvider.STRIPE,
        frozenset({"disputes:read"}),
    ),
    "stripe_list_disputes": ToolScopeRequirement(
        IntegrationProvider.STRIPE,
        frozenset({"disputes:read"}),
    ),
    "stripe_create_refund": ToolScopeRequirement(
        IntegrationProvider.STRIPE,
        frozenset({"refunds:write"}),
        write=True,
        irreversible=True,
    ),
    "gorgias_get_ticket": ToolScopeRequirement(
        IntegrationProvider.GORGIAS,
        frozenset({"tickets:read"}),
    ),
    "gorgias_search_customer": ToolScopeRequirement(
        IntegrationProvider.GORGIAS,
        frozenset({"customers:read"}),
    ),
    "gorgias_draft_reply": ToolScopeRequirement(
        IntegrationProvider.GORGIAS,
        frozenset({"tickets:read", "tickets:write"}),
        write=True,
    ),
    "shipbob_get_order": ToolScopeRequirement(IntegrationProvider.SHIPBOB, frozenset()),
    "shipbob_get_shipment": ToolScopeRequirement(IntegrationProvider.SHIPBOB, frozenset()),
    "shipbob_hold_order": ToolScopeRequirement(
        IntegrationProvider.SHIPBOB,
        frozenset(),
        write=True,
    ),
    "shipstation_get_order": ToolScopeRequirement(IntegrationProvider.SHIPSTATION, frozenset()),
    "shipstation_get_shipment": ToolScopeRequirement(
        IntegrationProvider.SHIPSTATION,
        frozenset(),
    ),
    "shipstation_hold_order": ToolScopeRequirement(
        IntegrationProvider.SHIPSTATION,
        frozenset(),
        write=True,
    ),
    "gmail_get_thread": ToolScopeRequirement(
        IntegrationProvider.GMAIL,
        frozenset({"https://www.googleapis.com/auth/gmail.readonly"}),
    ),
    "gmail_search_threads": ToolScopeRequirement(
        IntegrationProvider.GMAIL,
        frozenset({"https://www.googleapis.com/auth/gmail.readonly"}),
    ),
}


IMPLIED_SCOPES: dict[IntegrationProvider, dict[str, frozenset[str]]] = {
    IntegrationProvider.SHOPIFY: {
        "write_orders": frozenset({"read_orders"}),
        "write_fulfillments": frozenset({"read_fulfillments"}),
        "write_merchant_managed_fulfillment_orders": frozenset({"read_fulfillments"}),
    },
}


def required_scopes_for_provider(provider: IntegrationProvider) -> list[str]:
    scopes: set[str] = set()
    for requirement in TOOL_SCOPE_REQUIREMENTS.values():
        if requirement.provider is provider:
            scopes.update(requirement.scopes)
    return sorted(scopes)


def effective_granted_scopes(
    provider: IntegrationProvider,
    granted_scopes: list[str],
) -> list[str]:
    granted = set(granted_scopes)
    implied_by_scope = IMPLIED_SCOPES.get(provider, {})
    for scope in tuple(granted):
        granted.update(implied_by_scope.get(scope, frozenset()))
    return sorted(granted)


def tool_availability(
    tool_name: str,
    *,
    granted_scopes: list[str],
    settings: Settings,
) -> ToolAvailability | None:
    requirement = TOOL_SCOPE_REQUIREMENTS.get(tool_name)
    if requirement is None:
        return None
    granted = set(effective_granted_scopes(requirement.provider, granted_scopes))
    missing_scopes = sorted(scope for scope in requirement.scopes if scope not in granted)
    block_reasons: list[str] = []
    if missing_scopes:
        block_reasons.append(ToolBlockReason.MISSING_SCOPES.value)
    disabled_tools = _csv_set(settings.disabled_tools)
    if "*" in disabled_tools or tool_name in disabled_tools:
        block_reasons.append(ToolBlockReason.TOOL_DISABLED.value)
    if requirement.write and settings.global_provider_write_disable:
        block_reasons.append(ToolBlockReason.GLOBAL_WRITE_DISABLED.value)
    disabled_providers = _csv_set(settings.disabled_provider_writes)
    if requirement.write and (
        "*" in disabled_providers or requirement.provider.value in disabled_providers
    ):
        block_reasons.append(ToolBlockReason.PROVIDER_WRITE_DISABLED.value)
    return ToolAvailability(
        enabled=not block_reasons,
        provider=requirement.provider,
        missing_scopes=missing_scopes,
        block_reasons=block_reasons,
    )


def availability_by_tool(
    *,
    credentials_by_provider: Mapping[IntegrationProvider, list[str]],
    settings: Settings,
) -> dict[str, ToolAvailability]:
    return {
        tool_name: availability
        for tool_name in sorted(TOOL_SCOPE_REQUIREMENTS)
        if (
            availability := tool_availability(
                tool_name,
                granted_scopes=credentials_by_provider.get(
                    TOOL_SCOPE_REQUIREMENTS[tool_name].provider,
                    [],
                ),
                settings=settings,
            )
        )
        is not None
    }


def _csv_set(value: str) -> set[str]:
    return {
        token.strip()
        for token in value.replace("\n", ",").replace(" ", ",").split(",")
        if token.strip()
    }

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import httpx

from api.config import get_settings
from api.integrations.base import (
    IntegrationError,
    IntegrationErrorKind,
    IntegrationProvider,
    JsonValue,
    normalize_exception,
)


class ProviderHttpClient:
    def __init__(
        self,
        provider: IntegrationProvider,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._headers = dict(headers or {})
        self._timeout_seconds = timeout_seconds or settings.integration_http_timeout_seconds

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str | int | bool] | None = None,
        json_body: JsonValue = None,
        data: Mapping[str, str] | None = None,
    ) -> JsonValue:
        merged_headers = {**self._headers, **dict(headers or {})}
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers=merged_headers,
                    params=params,
                    json=json_body,
                    data=data,
                )
                response.raise_for_status()
                if not response.content:
                    return {}
                return cast(JsonValue, response.json())
        except Exception as exc:  # noqa: BLE001 - callers need normalized provider errors.
            normalized = normalize_exception(self._provider, exc)
            raise IntegrationError(
                normalized.kind,
                normalized.provider,
                normalized.message,
                status_code=normalized.status_code,
                retry_after=normalized.retry_after,
                details=normalized.details,
            ) from exc


def ensure_object(provider: IntegrationProvider, value: JsonValue) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise IntegrationError(
        kind=IntegrationErrorKind.FATAL,
        provider=provider,
        message="Expected JSON object response from provider.",
        details={"response_type": type(value).__name__},
    )

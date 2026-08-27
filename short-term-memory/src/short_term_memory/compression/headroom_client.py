"""HTTP adapter for a replaceable Headroom compression service."""

from copy import deepcopy
import logging
from time import perf_counter
from typing import Any, Mapping

import httpx

from short_term_memory.compression.telemetry import (
    HeadroomTelemetry,
    InMemoryHeadroomTelemetry,
)
from short_term_memory.models import (
    HeadroomCompressionResult,
    HeadroomCompressionStatus,
    HeadroomFailureReason,
)


_SCOPE_HEADER_NAMES = frozenset(
    {
        "x-headroom-user-id",
        "x-headroom-session-id",
        "x-headroom-project-id",
    }
)


class HeadroomHttpClient:
    """Call Headroom's public compression endpoint without importing Headroom."""

    def __init__(
        self,
        *,
        service_url: str | None,
        environment: str,
        timeout_seconds: float,
        telemetry: HeadroomTelemetry | None = None,
        http_client: httpx.Client | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        normalized = environment.casefold()
        if normalized not in {"development", "production"}:
            raise ValueError("environment must be development or production")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.service_url = service_url.rstrip("/") if service_url else None
        self.environment = normalized
        self.timeout_seconds = timeout_seconds
        self.telemetry = telemetry or InMemoryHeadroomTelemetry()
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client()
        self.logger = logger or logging.getLogger(__name__)

    def compress(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        model: str,
        correlation_id: str | None = None,
        scope_headers: Mapping[str, str] | None = None,
    ) -> HeadroomCompressionResult:
        original = tuple(deepcopy(message) for message in messages)
        headers = self._scope_headers(scope_headers)
        started_at = perf_counter()
        if self.service_url is None:
            return self._failure(
                original,
                reason=HeadroomFailureReason.SERVICE_UNAVAILABLE,
                error_type="MissingServiceUrl",
                correlation_id=correlation_id,
                started_at=started_at,
            )

        try:
            response = self.http_client.post(
                f"{self.service_url}/v1/compress",
                json={"messages": list(original), "model": model},
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            result = self._parse_response(response.json())
        except httpx.TimeoutException as exc:
            return self._failure(
                original,
                reason=HeadroomFailureReason.TIMEOUT,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
                started_at=started_at,
            )
        except httpx.HTTPStatusError as exc:
            return self._failure(
                original,
                reason=HeadroomFailureReason.HTTP_ERROR,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
                started_at=started_at,
            )
        except httpx.RequestError as exc:
            return self._failure(
                original,
                reason=HeadroomFailureReason.SERVICE_UNAVAILABLE,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
                started_at=started_at,
            )
        except (ValueError, TypeError, KeyError) as exc:
            return self._failure(
                original,
                reason=HeadroomFailureReason.INVALID_RESPONSE,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
                started_at=started_at,
            )
        except Exception as exc:
            return self._failure(
                original,
                reason=HeadroomFailureReason.UNEXPECTED_ERROR,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
                started_at=started_at,
            )

        self.telemetry.record_success(
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
        )
        if not result.compression_applied:
            self.telemetry.record_noop()
        return result

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    @staticmethod
    def _parse_response(payload: Any) -> HeadroomCompressionResult:
        if not isinstance(payload, dict):
            raise ValueError("invalid response object")
        messages = HeadroomHttpClient._messages(payload["messages"])
        tokens_before = HeadroomHttpClient._token(payload, "tokens_before")
        tokens_after = HeadroomHttpClient._token(payload, "tokens_after")
        tokens_saved = HeadroomHttpClient._token(payload, "tokens_saved")
        if tokens_saved != max(0, tokens_before - tokens_after):
            raise ValueError("invalid tokens_saved")
        compression_ratio = payload["compression_ratio"]
        if (
            isinstance(compression_ratio, bool)
            or not isinstance(compression_ratio, (int, float))
            or compression_ratio < 0
        ):
            raise ValueError("invalid compression_ratio")
        transforms = payload["transforms_applied"]
        if not isinstance(transforms, list) or not all(
            isinstance(item, str) for item in transforms
        ):
            raise ValueError("invalid transforms_applied")
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.SUCCESS,
            messages=messages,
            fallback_used=False,
            compression_applied=tokens_after < tokens_before,
            transforms_applied=tuple(transforms),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=tokens_saved,
        )

    @staticmethod
    def _messages(value: Any) -> tuple[dict[str, Any], ...]:
        if not isinstance(value, list):
            raise ValueError("invalid messages")
        messages: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("role"), str):
                raise ValueError("invalid message")
            messages.append(deepcopy(item))
        return tuple(messages)

    @staticmethod
    def _scope_headers(
        value: Mapping[str, str] | None,
    ) -> dict[str, str]:
        if value is None:
            return {}
        headers = dict(value)
        if set(headers) != _SCOPE_HEADER_NAMES:
            raise ValueError("invalid Headroom scope headers")
        if any(not isinstance(item, str) or not item.strip() for item in headers.values()):
            raise ValueError("invalid Headroom scope header value")
        return headers

    @staticmethod
    def _token(payload: dict[str, Any], name: str) -> int:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid {name}")
        return value

    def _failure(
        self,
        original: tuple[dict[str, Any], ...],
        *,
        reason: HeadroomFailureReason,
        error_type: str,
        correlation_id: str | None,
        started_at: float,
    ) -> HeadroomCompressionResult:
        fallback = self.environment == "development"
        self.telemetry.record_failure()
        if fallback:
            self.telemetry.record_fallback()
        self.logger.warning(
            "headroom_compression_failed reason=%s error_type=%s elapsed_ms=%.3f "
            "correlation_id=%s environment=%s fallback_used=%s",
            reason.value,
            error_type,
            (perf_counter() - started_at) * 1000,
            correlation_id,
            self.environment,
            fallback,
        )
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.FAILED,
            messages=original if fallback else (),
            fallback_used=fallback,
            failure_reason=reason,
        )

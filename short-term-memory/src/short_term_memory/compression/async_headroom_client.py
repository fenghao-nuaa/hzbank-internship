"""Async HTTP boundary for the official Headroom compression API."""

from copy import deepcopy
from typing import Any, Mapping

import httpx

from short_term_memory.compression.headroom_client import HeadroomHttpClient
from short_term_memory.models import (
    HeadroomCompressionResult,
    HeadroomCompressionStatus,
    HeadroomFailureReason,
)


class AsyncHeadroomClient:
    """One shared async client; retry policy belongs to the persistent queue."""

    def __init__(
        self,
        service_url: str,
        *,
        timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not service_url.strip():
            raise ValueError("service_url must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.service_url = service_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._owns_http_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
            timeout=timeout_seconds,
            transport=transport,
        )

    async def compress(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        model: str,
        correlation_id: str | None = None,
        scope_headers: Mapping[str, str] | None = None,
    ) -> HeadroomCompressionResult:
        """Return a typed failure without inspecting content or retrying requests."""
        del correlation_id  # Correlation is owned by callers' telemetry boundary.
        original = tuple(deepcopy(message) for message in messages)
        try:
            response = await self.http.post(
                f"{self.service_url}/v1/compress",
                json={"messages": list(original), "model": model},
                headers=HeadroomHttpClient._scope_headers(scope_headers),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return HeadroomHttpClient._parse_response(response.json())
        except httpx.TimeoutException:
            return self._failure(HeadroomFailureReason.TIMEOUT)
        except httpx.HTTPStatusError:
            return self._failure(HeadroomFailureReason.HTTP_ERROR)
        except httpx.RequestError:
            return self._failure(HeadroomFailureReason.SERVICE_UNAVAILABLE)
        except (TypeError, ValueError, KeyError):
            return self._failure(HeadroomFailureReason.INVALID_RESPONSE)
        except Exception:
            return self._failure(HeadroomFailureReason.UNEXPECTED_ERROR)

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self.http.aclose()

    @staticmethod
    def _failure(reason: HeadroomFailureReason) -> HeadroomCompressionResult:
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.FAILED,
            messages=(),
            fallback_used=False,
            failure_reason=reason,
        )

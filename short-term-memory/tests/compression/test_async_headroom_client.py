import json

import httpx
import pytest

from short_term_memory.compression.async_headroom_client import AsyncHeadroomClient
from short_term_memory.models import HeadroomCompressionStatus, HeadroomFailureReason
from tests.factories import scope_headers


class RecordingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, compressed_messages):
        self.compressed_messages = compressed_messages
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "messages": self.compressed_messages,
                "tokens_before": 100,
                "tokens_after": 25,
                "tokens_saved": 75,
                "compression_ratio": 4.0,
                "transforms_applied": ["test"],
            },
            request=request,
        )


@pytest.mark.asyncio
async def test_async_headroom_sends_only_candidate_originals():
    transport = RecordingAsyncTransport(
        compressed_messages=[{"role": "system", "content": "marker"}]
    )
    client = AsyncHeadroomClient(
        "http://headroom:8787", timeout_seconds=5, transport=transport
    )

    result = await client.compress(
        ({"role": "user", "content": "ORIGINAL"},),
        model="deepseek-v4-flash",
        correlation_id="req-1",
        scope_headers=scope_headers(),
    )

    assert transport.requests[0].url.path == "/v1/compress"
    assert json.loads(transport.requests[0].content)["messages"] == [
        {"role": "user", "content": "ORIGINAL"}
    ]
    assert result.status is HeadroomCompressionStatus.SUCCESS
    assert result.messages[0]["content"] == "marker"
    await client.aclose()


class TimeoutTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx.ReadTimeout("private", request=request)


@pytest.mark.asyncio
async def test_async_headroom_maps_timeout_to_typed_failure():
    client = AsyncHeadroomClient(
        "http://headroom:8787", timeout_seconds=5, transport=TimeoutTransport()
    )

    result = await client.compress(
        ({"role": "user", "content": "ORIGINAL"},),
        model="deepseek-v4-flash",
        scope_headers=scope_headers(),
    )

    assert result.status is HeadroomCompressionStatus.FAILED
    assert result.failure_reason is HeadroomFailureReason.TIMEOUT
    assert result.messages == ()
    await client.aclose()

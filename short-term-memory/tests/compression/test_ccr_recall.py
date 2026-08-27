"""Unit tests for application-driven CCR recall (CcrRecallClient)."""

import httpx
import pytest

from short_term_memory.compression.ccr_recall import (
    CcrRecallClient,
    CcrRecallInvalidError,
    CcrRecallNotFoundError,
    CcrRecallUnavailableError,
    extract_marker_hashes,
)

SCOPE = {
    "x-headroom-user-id": "dream-v1-test-user",
    "x-headroom-session-id": "dream-v1-test-session",
    "x-headroom-project-id": "dream-v1-test-project",
}


class TestExtractMarkerHashes:
    def test_retrieve_more_hash(self) -> None:
        messages = [
            {"role": "assistant", "content": "x [500 items compressed to 20. Retrieve more: hash=abc123def456] y"}
        ]
        assert extract_marker_hashes(messages) == ("abc123def456",)

    def test_retrieve_original_hash(self) -> None:
        messages = [
            {"role": "assistant", "content": "Retrieve original: hash=fff789abc123"}
        ]
        assert extract_marker_hashes(messages) == ("fff789abc123",)

    def test_inline_ccr(self) -> None:
        messages = [{"role": "tool", "content": "wrap <<ccr:hash999888777 end"}]
        assert extract_marker_hashes(messages) == ("hash999888777",)

    def test_deduplicates_and_preserves_order(self) -> None:
        messages = [
            {"role": "a", "content": "Retrieve more: hash=aaa111bbb222 and Retrieve more: hash=ccc333ddd444"},
            {"role": "b", "content": "Retrieve more: hash=aaa111bbb222"},
        ]
        assert extract_marker_hashes(messages) == ("aaa111bbb222", "ccc333ddd444")

    def test_ignores_non_string_content(self) -> None:
        messages = [
            {"role": "a", "content": None},
            {"role": "b", "content": 123},
            {"role": "c", "content": "no marker here"},
        ]
        assert extract_marker_hashes(messages) == ()

    def test_empty_input(self) -> None:
        assert extract_marker_hashes([]) == ()
        assert extract_marker_hashes(None) == ()


@pytest.mark.asyncio
async def test_recall_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/retrieve"
        body = request.read().decode()
        assert "abc123def456" in body
        return httpx.Response(
            200,
            json={"hash": "abc123def456", "original_content": "ORIGINAL_ANCHOR_7391"},
        )

    client = CcrRecallClient(
        "http://headroom:8787",
        timeout_seconds=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        content = await client.recall("abc123def456", scope_headers=SCOPE)
        assert content == "ORIGINAL_ANCHOR_7391"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_recall_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = CcrRecallClient(
        "http://headroom:8787",
        timeout_seconds=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(CcrRecallNotFoundError):
            await client.recall("abc123def456", scope_headers=SCOPE)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_recall_unavailable_on_connection_error() -> None:
    async def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = CcrRecallClient(
        "http://headroom:8787",
        timeout_seconds=1,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(raise_connect)),
    )
    try:
        with pytest.raises(CcrRecallUnavailableError):
            await client.recall("abc123def456", scope_headers=SCOPE)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_recall_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"wrong": "shape"})

    client = CcrRecallClient(
        "http://headroom:8787",
        timeout_seconds=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(CcrRecallInvalidError):
            await client.recall("abc123def456", scope_headers=SCOPE)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_recall_blank_hash_rejected() -> None:
    client = CcrRecallClient("http://headroom:8787", timeout_seconds=5)
    try:
        with pytest.raises(CcrRecallInvalidError):
            await client.recall("   ", scope_headers=SCOPE)
    finally:
        await client.aclose()

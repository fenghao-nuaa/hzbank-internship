"""Unit tests for AgentChatClient orchestration."""

from __future__ import annotations

import json

import httpx
import pytest

from short_term_memory.agent.agent_chat import (
    MEMORY_TOOL_DEFINITIONS,
    AgentChatClient,
)


def _recall_handler(request: httpx.Request) -> httpx.Response:
    """Mock memory API handler: read returns markers, recall returns original."""
    path = request.url.path
    if path == "/v1/memories/activate":
        return httpx.Response(200, json={"recovered": False, "latest_sequence": 0})
    if path in {"/v1/memories/read", "/v1/memories/prepare"}:
        return httpx.Response(
            200,
            json={
                "request_id": "r1",
                "messages": [
                    {"role": "system", "content": "continuity summary"},
                    {
                        "role": "assistant",
                        "content": "[20 items compressed. Retrieve more: hash=abc123]",
                    },
                ],
                "memory": {"source": "redis", "compression_segments": 1},
                "headroom": {
                    "proxy_url": "http://headroom:8787/v1",
                    "scope_headers": {"x-headroom-user-id": "u"},
                },
                "ccr_markers": ["abc123"],
                "tools": list(MEMORY_TOOL_DEFINITIONS[:-1]),
            },
        )
    if path == "/v1/memories/recall":
        return httpx.Response(
            200,
            json={
                "results": [
                    {"hash": "abc123", "content": "ORIGINAL_ANCHOR_7391", "recovered": True}
                ]
            },
        )
    if path == "/v1/memories/write":
        return httpx.Response(200, json={"request_id": "w", "accepted": True})
    return httpx.Response(404)


class RecordingModelCall:
    """Fake model: first call requests headroom_retrieve, second answers."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def __call__(self, **kwargs):
        messages = kwargs["messages"]
        self.calls.append(messages)
        # First call -> request headroom_retrieve
        if len(self.calls) == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "headroom_retrieve",
                            "arguments": json.dumps({"hash": "abc123"}),
                        },
                    }
                ],
            }
        # Second call -> final answer, should contain recalled original
        return {"content": "BASED_ON_RECALLED_ANCHOR", "tool_calls": []}


@pytest.mark.asyncio
async def test_agent_chat_handles_recall_tool_call_loop() -> None:
    model = RecordingModelCall()
    client = AgentChatClient(
        memory_api_url="http://test",
        model_call=model,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_recall_handler)),
    )
    try:
        answer = await client.turn("u-1", "s-1", "那个文档里的细节是什么")
    finally:
        await client.aclose()

    assert answer == "BASED_ON_RECALLED_ANCHOR"
    # model called twice (tool round + final)
    assert len(model.calls) == 2
    # second call should include the tool result with recalled original
    second_messages = model.calls[1]
    assert any(
        m.get("role") == "tool" and "ORIGINAL_ANCHOR_7391" in str(m.get("content", ""))
        for m in second_messages
    )


@pytest.mark.asyncio
async def test_preview_history_returns_compressed_view() -> None:
    client = AgentChatClient(
        memory_api_url="http://test",
        model_call=lambda **kw: {"content": "x", "tool_calls": []},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_recall_handler)),
    )
    try:
        preview = await client.preview_history("u-1", "s-1")
    finally:
        await client.aclose()

    assert preview["user_id"] == "u-1"
    assert preview["session_id"] == "s-1"
    assert len(preview["messages"]) > 0
    assert preview["ccr_markers"] == ["abc123"]


def test_format_history_preview_shows_full_content() -> None:
    long_content = "X" * 500
    preview = {
        "session_id": "s-1",
        "messages": [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": "short"},
        ],
        "ccr_markers": ["abc123"],
    }
    text = AgentChatClient.format_history_preview(preview)
    # Full content present, no truncation.
    assert long_content in text
    assert "…" not in text
    assert "[user]" in text
    assert "[assistant]" in text


def test_format_history_preview_empty() -> None:
    text = AgentChatClient.format_history_preview(
        {"session_id": "s-1", "messages": [], "ccr_markers": []}
    )
    assert "无历史记忆" in text


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


class ScriptedModel:
    def __init__(self, completions: list[dict]) -> None:
        self.completions = completions
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.completions[len(self.calls) - 1]


class RecordingTranscriptTransport:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.requests.append(request)
        if request.url.path == "/v1/memories/activate":
            return httpx.Response(200, json={"recovered": False, "latest_sequence": 0})
        if request.url.path == "/v1/memories/prepare":
            return httpx.Response(
                200,
                json={
                    "messages": [{"role": "user", "content": "之前 TTL 是多少？"}],
                    "ccr_markers": [],
                    "headroom": {
                        "proxy_url": "http://headroom/v1",
                        "scope_headers": {
                            "x-headroom-session-id": "opaque-session"
                        },
                    },
                    "tools": list(MEMORY_TOOL_DEFINITIONS[:-1]),
                },
            )
        if request.url.path == "/v1/memories/transcript/grep":
            return httpx.Response(200, json={"content": "84\tTTL was discussed"})
        if request.url.path == "/v1/memories/transcript/read":
            return httpx.Response(200, json={"content": "84\tTTL is 43200"})
        if request.url.path == "/v1/memories/write":
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(404)


@pytest.mark.asyncio
async def test_agent_autonomously_greps_then_reads_before_answering() -> None:
    model = ScriptedModel(
        [
            tool_call(
                "g1",
                "Grep",
                {
                    "path": "journal://current-session",
                    "pattern": "TTL",
                    "output_mode": "content",
                },
            ),
            tool_call(
                "r1",
                "Read",
                {
                    "file_path": "journal://current-session",
                    "offset": 84,
                    "limit": 8,
                },
            ),
            {"content": "之前确定的 TTL 是 43200 秒。", "tool_calls": []},
        ]
    )
    transport = RecordingTranscriptTransport()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as http:
        client = AgentChatClient(
            memory_api_url="http://memory", model_call=model, http_client=http
        )
        answer = await client.turn("u", "s", "之前 TTL 是多少？")

    assert answer == "之前确定的 TTL 是 43200 秒。"
    assert transport.paths[-3:-1] == [
        "/v1/memories/transcript/grep",
        "/v1/memories/transcript/read",
    ]
    assert all(call["tools"] == MEMORY_TOOL_DEFINITIONS for call in model.calls)
    assert model.calls[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "g1",
        "content": "84\tTTL was discussed",
    }
    assert model.calls[2]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "r1",
        "content": "84\tTTL is 43200",
    }
    assert model.calls[1]["messages"][-2]["tool_calls"][0]["id"] == "g1"
    assert model.calls[2]["messages"][-2]["tool_calls"][0]["id"] == "r1"
    assert not any(
        "关键词" in str(message.get("content", ""))
        or "headroom_retrieve` 工具" in str(message.get("content", ""))
        for message in model.calls[0]["messages"]
    )
    for request in transport.requests:
        if request.url.path.startswith("/v1/memories/transcript/"):
            assert request.headers["x-memory-session-scope"] == "opaque-session"


@pytest.mark.asyncio
async def test_turn_activates_historical_session_before_user_write() -> None:
    transport = RecordingTranscriptTransport()
    model = ScriptedModel([{"content": "continued", "tool_calls": []}])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as http:
        client = AgentChatClient(
            memory_api_url="http://memory", model_call=model, http_client=http
        )
        await client.turn("u", "historical", "continue", history_turns=5)

    assert transport.paths[:3] == [
        "/v1/memories/activate",
        "/v1/memories/write",
        "/v1/memories/prepare",
    ]
    activation = json.loads(transport.requests[0].content)
    assert activation == {
        "user_id": "u",
        "session_id": "historical",
        "history_turns": 5,
    }

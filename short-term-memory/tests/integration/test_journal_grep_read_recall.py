import json

import httpx
import pytest

from short_term_memory.agent.agent_chat import AgentChatClient


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class Model:
    def __init__(self) -> None:
        self.calls = []
        self.outputs = iter(
            (
                tool_call(
                    "grep-a",
                    "Grep",
                    {
                        "path": "journal://current-session",
                        "pattern": "exact-7391",
                        "output_mode": "content",
                    },
                ),
                tool_call(
                    "read-a",
                    "Read",
                    {
                        "file_path": "journal://current-session",
                        "offset": 1,
                        "limit": 2,
                    },
                ),
                {"content": "A 的准确原文是 ORIGINAL A exact-7391。", "tool_calls": []},
            )
        )

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.outputs)


class Transport:
    def __init__(self) -> None:
        self.paths = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/v1/memories/activate":
            return httpx.Response(
                200, json={"recovered": False, "latest_sequence": 0}
            )
        if request.url.path == "/v1/memories/write":
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/v1/memories/prepare":
            return httpx.Response(
                200,
                json={
                    "messages": [{"role": "user", "content": "continuity summary ABCDE"}],
                    "headroom": {
                        "proxy_url": "http://headroom/v1",
                        "scope_headers": {"x-headroom-session-id": "opaque-scope"},
                    },
                    "tools": [
                        {"type": "function", "function": {"name": "Grep"}},
                        {"type": "function", "function": {"name": "Read"}},
                    ],
                },
            )
        if request.url.path == "/v1/memories/transcript/grep":
            assert request.headers["x-memory-session-scope"] == "opaque-scope"
            return httpx.Response(200, json={"content": "1\tORIGINAL A exact-7391"})
        if request.url.path == "/v1/memories/transcript/read":
            assert request.headers["x-memory-session-scope"] == "opaque-scope"
            return httpx.Response(200, json={"content": "1\tORIGINAL A exact-7391"})
        return httpx.Response(404)


@pytest.mark.asyncio
async def test_agent_automatically_greps_then_reads_journal_for_exact_a_detail() -> None:
    model = Model()
    transport = Transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AgentChatClient(
            memory_api_url="http://memory", model_call=model, http_client=http
        )
        answer = await client.turn("u", "s", "A 的准确原文是什么？")

    assert answer == "A 的准确原文是 ORIGINAL A exact-7391。"
    assert transport.paths[-3:-1] == [
        "/v1/memories/transcript/grep",
        "/v1/memories/transcript/read",
    ]
    assert model.calls[1]["messages"][-1]["content"] == "1\tORIGINAL A exact-7391"
    assert model.calls[2]["messages"][-1]["content"] == "1\tORIGINAL A exact-7391"

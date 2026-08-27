from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from examples.deepseek_chat import run_turn


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.raise_count = 0

    def raise_for_status(self) -> None:
        self.raise_count += 1

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeHttpClient:
    def __init__(self, trace: list[str], **kwargs: Any) -> None:
        self.trace = trace
        self.kwargs = kwargs
        self.requests: list[dict[str, Any]] = []
        self.responses = [
            FakeResponse({"accepted": True}),
            FakeResponse(
                {
                    "messages": [{"role": "user", "content": "memory context"}],
                    "headroom": {
                        "proxy_url": "http://headroom:8787/v1",
                        "scope_headers": {"x-headroom-session-id": "opaque-scope"},
                    },
                }
            ),
            FakeResponse({"accepted": True}),
        ]

    def __enter__(self) -> FakeHttpClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, path: str, **kwargs: Any) -> FakeResponse:
        self.trace.append(path)
        self.requests.append({"path": path, **kwargs})
        return self.responses[len(self.requests) - 1]


class FakeOpenAI:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.kwargs: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_completion)
        )

    def factory(self, **kwargs: Any) -> FakeOpenAI:
        self.kwargs = kwargs
        return self

    def _create_completion(self, **kwargs: Any) -> Any:
        self.trace.append("deepseek")
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="assistant answer"))
            ]
        )


def build_fakes() -> tuple[
    list[str], FakeHttpClient, Callable[..., FakeHttpClient], FakeOpenAI
]:
    trace: list[str] = []
    client = FakeHttpClient(trace)

    def http_factory(**kwargs: Any) -> FakeHttpClient:
        client.kwargs = kwargs
        return client

    return trace, client, http_factory, FakeOpenAI(trace)


def test_run_turn_writes_user_reads_memory_calls_proxy_then_writes_assistant() -> None:
    trace, memory, http_factory, openai = build_fakes()
    output: list[str] = []

    answer = run_turn(
        memory_api_url="http://memory-api:8080",
        user_id="u",
        session_id="s",
        prompt="What is the anchor?",
        deepseek_api_key="test-key",
        http_factory=http_factory,
        openai_factory=openai.factory,
        print_fn=output.append,
    )

    assert trace == [
        "/v1/memories/write",
        "/v1/memories/read",
        "deepseek",
        "/v1/memories/write",
    ]
    assert openai.kwargs == {
        "api_key": "test-key",
        "base_url": "http://headroom:8787/v1",
        "default_headers": {"x-headroom-session-id": "opaque-scope"},
    }
    assert openai.requests == [
        {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "memory context"}],
        }
    ]
    assert answer == "assistant answer"
    assert output[0] == "assistant answer"
    assert output[1].startswith("timings_ms=")

    user_event = memory.requests[0]["json"]["events"][0]
    assistant_event = memory.requests[2]["json"]["events"][0]
    assert UUID(user_event["event_id"])
    assert UUID(assistant_event["event_id"])
    assert user_event == {
        "event_id": user_event["event_id"],
        "role": "user",
        "content_type": "conversation",
        "content": "What is the anchor?",
        "metadata": {},
    }
    assert assistant_event == {
        "event_id": assistant_event["event_id"],
        "role": "assistant",
        "content_type": "conversation",
        "content": "assistant answer",
        "metadata": {},
    }
    assert memory.responses[0].raise_count == 1
    assert memory.responses[1].raise_count == 1
    assert memory.responses[2].raise_count == 1


def test_run_turn_never_sends_or_prints_deepseek_key() -> None:
    _, memory, http_factory, openai = build_fakes()
    output: list[str] = []
    secret = "deepseek-secret-never-leak"

    run_turn(
        memory_api_url="http://memory-api:8080",
        user_id="u",
        session_id="s",
        prompt="hello",
        deepseek_api_key=secret,
        memory_api_auth_token="memory-token",
        http_factory=http_factory,
        openai_factory=openai.factory,
        print_fn=output.append,
    )

    assert memory.kwargs["headers"] == {"Authorization": "Bearer memory-token"}
    assert secret not in repr(memory.kwargs)
    assert secret not in repr(memory.requests)
    assert secret not in "\n".join(output)


def test_memory_service_import_does_not_import_openai() -> None:
    code = (
        "import sys; import short_term_memory.service.app; "
        "assert 'openai' not in sys.modules"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        env={**os.environ, "PYTHONPATH": "src"},
    )


def test_help_does_not_require_deepseek_api_key() -> None:
    project_root = Path(__file__).parents[2]
    env = {**os.environ, "PYTHONPATH": "src"}
    env.pop("DEEPSEEK_API_KEY", None)

    result = subprocess.run(
        [sys.executable, "examples/deepseek_chat.py", "--help"],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--memory-api-url" in result.stdout
    assert "--user-id" in result.stdout
    assert "--session-id" in result.stdout
    assert "--prompt" in result.stdout
    assert "--model" in result.stdout

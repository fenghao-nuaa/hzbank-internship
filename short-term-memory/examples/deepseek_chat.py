"""One DeepSeek chat turn routed through Headroom-backed memory context."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import os
import time
from typing import Any
from uuid import uuid4

import httpx


DEFAULT_MODEL = "deepseek-v4-flash"


def _default_openai_factory(**kwargs: Any) -> Any:
    from openai import OpenAI

    return OpenAI(**kwargs)


def _write_payload(
    user_id: str, session_id: str, role: str, content: str
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "session_id": session_id,
        "events": [
            {
                "event_id": str(uuid4()),
                "role": role,
                "content_type": "conversation",
                "content": content,
                "metadata": {},
            }
        ],
    }


def _post_json(client: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=payload)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError(f"memory API returned a non-object response for {path}")
    return result


def run_turn(
    memory_api_url: str,
    user_id: str,
    session_id: str,
    prompt: str,
    deepseek_api_key: str,
    model: str = DEFAULT_MODEL,
    *,
    memory_api_auth_token: str | None = None,
    http_factory: Callable[..., Any] = httpx.Client,
    openai_factory: Callable[..., Any] | None = None,
    print_fn: Callable[[str], None] = print,
) -> str:
    """Persist one user/assistant turn around an independent DeepSeek call."""

    timings: dict[str, float] = {}
    total_started = time.perf_counter()
    headers = (
        {"Authorization": f"Bearer {memory_api_auth_token}"}
        if memory_api_auth_token
        else {}
    )

    with http_factory(
        base_url=memory_api_url.rstrip("/"), headers=headers, timeout=30.0
    ) as memory_client:
        started = time.perf_counter()
        _post_json(
            memory_client,
            "/v1/memories/write",
            _write_payload(user_id, session_id, "user", prompt),
        )
        timings["memory_write_user"] = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        memory = _post_json(
            memory_client,
            "/v1/memories/read",
            {"user_id": user_id, "session_id": session_id},
        )
        timings["memory_read"] = (time.perf_counter() - started) * 1_000

        headroom = memory.get("headroom")
        messages = memory.get("messages")
        if not isinstance(headroom, Mapping) or not isinstance(messages, list):
            raise RuntimeError(
                "memory API response omitted messages or Headroom context"
            )
        proxy_url = headroom.get("proxy_url")
        scope_headers = headroom.get("scope_headers")
        if not isinstance(proxy_url, str) or not isinstance(scope_headers, dict):
            raise RuntimeError("memory API returned invalid Headroom context")

        started = time.perf_counter()
        factory = openai_factory or _default_openai_factory
        deepseek = factory(
            api_key=deepseek_api_key,
            base_url=proxy_url,
            default_headers=scope_headers,
        )
        completion = deepseek.chat.completions.create(
            model=model,
            messages=messages,
        )
        timings["deepseek"] = (time.perf_counter() - started) * 1_000
        answer = completion.choices[0].message.content
        if not isinstance(answer, str) or not answer:
            raise RuntimeError("DeepSeek returned an empty answer")

        started = time.perf_counter()
        _post_json(
            memory_client,
            "/v1/memories/write",
            _write_payload(user_id, session_id, "assistant", answer),
        )
        timings["memory_write_assistant"] = (time.perf_counter() - started) * 1_000

    timings["total"] = (time.perf_counter() - total_started) * 1_000
    print_fn(answer)
    print_fn(
        "timings_ms="
        + ",".join(f"{name}:{value:.3f}" for name, value in timings.items())
    )
    return answer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one DeepSeek turn through Headroom memory context."
    )
    parser.add_argument(
        "--memory-api-url",
        default=os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8080"),
        help="Short-term memory API base URL.",
    )
    parser.add_argument("--user-id", required=True, help="Memory user ID.")
    parser.add_argument("--session-id", required=True, help="Memory session ID.")
    parser.add_argument("--prompt", required=True, help="User prompt.")
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
        help=f"DeepSeek model (default: {DEFAULT_MODEL}).",
    )
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not deepseek_api_key:
        parser.error("DEEPSEEK_API_KEY is required")

    run_turn(
        memory_api_url=args.memory_api_url,
        user_id=args.user_id,
        session_id=args.session_id,
        prompt=args.prompt,
        deepseek_api_key=deepseek_api_key,
        model=args.model,
        memory_api_auth_token=os.environ.get("MEMORY_API_AUTH_TOKEN"),
    )


if __name__ == "__main__":
    main()

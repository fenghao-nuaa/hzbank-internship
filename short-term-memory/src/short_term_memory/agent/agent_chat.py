"""Agent-facing chat orchestration built on the short-term memory service.

This is the "方案2" deliverable: instead of each integrator copying chat_loop,
they can import :class:`AgentChatClient` and get the full loop:

  activate session -> write user message -> read context -> call model -> execute memory tools
  -> append tool results -> call the same model -> write assistant answer

The memory API is called over HTTP (httpx). The model provider is injected via
``model_call`` so the memory service source does not hard-depend on the OpenAI
SDK; the example wires it to the OpenAI client through Headroom proxy.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

import httpx

from short_term_memory.transcript.tool_definitions import (
    TRANSCRIPT_TOOL_DEFINITIONS,
)


HEADROOM_RETRIEVE_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "headroom_retrieve",
        "description": (
            "Retrieve the exact original content for a Headroom CCR marker hash."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"hash": {"type": "string", "minLength": 1}},
            "required": ["hash"],
        },
    },
}
MEMORY_TOOL_DEFINITIONS = (
    *TRANSCRIPT_TOOL_DEFINITIONS,
    HEADROOM_RETRIEVE_TOOL_DEFINITION,
)


class AgentChatClient:
    """Complete chat turn orchestration over the memory service.

    Typical usage (see examples/chat_loop.py):

        client = AgentChatClient(
            memory_api_url="http://127.0.0.1:8080",
            model_call=model_call,   # async fn(messages, **kw) -> str
            auth_token=...,
        )
        answer = await client.turn("u-001", "s-001", "你好")
    """

    def __init__(
        self,
        *,
        memory_api_url: str,
        model_call: Callable[..., Any],
        auth_token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_tool_rounds: int = 5,
        context_window_tokens: int = 128_000,
        max_output_tokens: int = 8_192,
        prepare_timeout_seconds: float = 300.0,
    ) -> None:
        if min(context_window_tokens, max_output_tokens) < 1:
            raise ValueError("model token limits must be positive")
        if prepare_timeout_seconds <= 0:
            raise ValueError("prepare_timeout_seconds must be positive")
        self.memory_api_url = memory_api_url.rstrip("/")
        self.model_call = model_call
        self.max_tool_rounds = max_tool_rounds
        self.model_profile = {
            "context_window_tokens": context_window_tokens,
            "max_output_tokens": max_output_tokens,
        }
        self.prepare_timeout_seconds = prepare_timeout_seconds
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        self._owns_http = http_client is None
        self.http = http_client or httpx.AsyncClient(headers=headers, timeout=60.0)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.memory_api_url}{path}"
        response = await self.http.post(
            url,
            json=dict(payload),
            headers=None if headers is None else dict(headers),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    async def turn(
        self,
        user_id: str,
        session_id: str,
        prompt: str,
        *,
        model: str | None = None,
        content_type: str = "conversation",
        event_id: str | None = None,
        history_turns: int | None = None,
    ) -> str:
        """Activate, persist the user message, prepare, run tools, and persist reply."""
        from uuid import uuid4

        # 1. Restore a bounded historical projection before reserving a sequence.
        await self._post(
            "/v1/memories/activate",
            {
                "user_id": user_id,
                "session_id": session_id,
                "history_turns": history_turns,
            },
        )

        # 2. write the user message.
        await self._post(
            "/v1/memories/write",
            {
                "user_id": user_id,
                "session_id": session_id,
                "events": [
                    {
                        "event_id": event_id or uuid4().hex,
                        "role": "user",
                        "content_type": content_type,
                        "content": prompt,
                        "metadata": {},
                    }
                ],
            },
        )

        # 3. prepare the current context, including request-time L2/L3/L4 compact.
        memory = await self._post(
            "/v1/memories/prepare",
            {
                "user_id": user_id,
                "session_id": session_id,
                "history_turns": history_turns,
                "model_profile": self.model_profile,
                "query_source": "main",
            },
            timeout=self.prepare_timeout_seconds,
        )
        messages = list(memory.get("messages") or [])
        headroom = memory.get("headroom") or {}
        proxy_url = headroom.get("proxy_url")
        scope_headers = headroom.get("scope_headers") or {}
        tools = tuple(memory.get("tools") or ()) + (
            HEADROOM_RETRIEVE_TOOL_DEFINITION,
        )

        # 4. call the model (may loop on tool calls).
        answer = await self._ask(
            messages,
            proxy_url,
            scope_headers,
            model,
            user_id,
            session_id,
            tools,
        )

        # 5. write the assistant answer back.
        await self._post(
            "/v1/memories/write",
            {
                "user_id": user_id,
                "session_id": session_id,
                "events": [
                    {
                        "event_id": uuid4().hex,
                        "role": "assistant",
                        "content_type": "conversation",
                        "content": answer,
                        "metadata": {},
                    }
                ],
            },
        )
        return answer

    async def preview_history(
        self,
        user_id: str,
        session_id: str,
        *,
        history_turns: int | None = None,
    ) -> dict[str, Any]:
        """Preview a historical session's compressed view without restoring originals.

        Calls read with ``history=true`` so it returns only the compressed summary
        (semantic summary + compressed segments with markers), never the full
        original journal — opening a historical session cannot fill the context.
        """
        memory = await self._post(
            "/v1/memories/read",
            {
                "user_id": user_id,
                "session_id": session_id,
                "history_turns": history_turns,
                "history": True,
            },
        )
        messages = list(memory.get("messages") or [])
        ccr_markers = list(memory.get("ccr_markers") or [])
        memory_state = memory.get("memory") or {}
        return {
            "user_id": user_id,
            "session_id": session_id,
            "messages": messages,
            "ccr_markers": ccr_markers,
            "compressed_through_sequence": memory_state.get("compressed_through_sequence"),
            "compression_segments": memory_state.get("compression_segments"),
            "source": memory_state.get("source"),
        }

    @staticmethod
    def format_history_preview(preview: Mapping[str, Any]) -> str:
        """Render a history preview as full (untruncated) text for display."""
        messages = preview.get("messages") or []
        markers = preview.get("ccr_markers") or []
        if not messages:
            return f"[history] 会话 {preview.get('session_id')} 无历史记忆（新会话）"
        lines: list[str] = [
            f"[history] 历史会话 {preview.get('session_id')} 的压缩上下文 "
            f"（{len(messages)} 条，{len(markers)} 个可召回标记）：",
            "─" * 60,
        ]
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str):
                lines.append(f"  [{role}]")
                lines.append(f"  {content}")
            else:
                lines.append(f"  [{role}] (非文本内容: {type(content).__name__})")
        lines.append("─" * 60)
        return "\n".join(lines)

    async def _ask(
        self,
        messages: list[dict[str, Any]],
        proxy_url: str | None,
        scope_headers: dict[str, str],
        model: str | None,
        user_id: str,
        session_id: str,
        tools: tuple[dict[str, Any], ...] = MEMORY_TOOL_DEFINITIONS,
    ) -> str:
        working = list(messages)
        session_scope = str(scope_headers.get("x-headroom-session-id", ""))

        for _round in range(self.max_tool_rounds):
            completion = await self.model_call(
                messages=list(working),
                model=model,
                proxy_url=proxy_url,
                scope_headers=scope_headers,
                tools=tools,
            )
            content = completion.get("content")
            tool_calls = completion.get("tool_calls") or []

            if tool_calls:
                # Append assistant tool-call message.
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.get("id"),
                        "type": "function",
                        "function": {
                            "name": tc.get("function", {}).get("name"),
                            "arguments": tc.get("function", {}).get("arguments") or "{}",
                        },
                    }
                    for tc in tool_calls
                ]
                working.append(assistant_msg)

                for tc in tool_calls:
                    name = tc.get("function", {}).get("name")
                    raw_arguments = tc.get("function", {}).get("arguments") or "{}"
                    try:
                        args = (
                            dict(raw_arguments)
                            if isinstance(raw_arguments, Mapping)
                            else json.loads(raw_arguments)
                        )
                    except (json.JSONDecodeError, TypeError, ValueError):
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    tool_content = await self._execute_tool(
                        name=str(name),
                        arguments=args,
                        user_id=user_id,
                        session_id=session_id,
                        session_scope=session_scope,
                    )
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "content": tool_content,
                        }
                    )
                continue

            if not isinstance(content, str) or not content:
                raise RuntimeError("model returned an empty answer")
            return content

        raise RuntimeError("model exceeded tool-call rounds without a final answer")

    async def _execute_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        user_id: str,
        session_id: str,
        session_scope: str,
    ) -> str:
        if name == "headroom_retrieve":
            return (
                await self._recall(
                    user_id, session_id, str(arguments.get("hash", ""))
                )
                or "not found"
            )
        if name == "Grep":
            body = await self._post(
                "/v1/memories/transcript/grep",
                {**arguments, "user_id": user_id, "session_id": session_id},
                headers={"X-Memory-Session-Scope": session_scope},
            )
            return str(body["content"])
        if name == "Read":
            body = await self._post(
                "/v1/memories/transcript/read",
                {**arguments, "user_id": user_id, "session_id": session_id},
                headers={"X-Memory-Session-Scope": session_scope},
            )
            return str(body["content"])
        return f"unknown tool {name}"

    async def _recall(self, user_id: str, session_id: str, hash_value: str) -> str | None:
        """Fetch the original content for a marker hash via the recall endpoint."""
        if not hash_value:
            return None
        body = await self._post(
            "/v1/memories/recall",
            {"user_id": user_id, "session_id": session_id, "hashes": [hash_value]},
        )
        results = body.get("results") or []
        for result in results:
            if result.get("recovered"):
                return result.get("content")
        return None

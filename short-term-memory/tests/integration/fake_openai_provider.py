"""Deterministic OpenAI-compatible upstream for official Headroom CCR tests."""

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
import json
import re
from threading import Lock
from typing import Any


HASH_PATTERN = re.compile(r"(?:hash=|<<ccr:)([A-Za-z0-9_-]{1,128})")
calls: list[dict[str, Any]] = []
calls_lock = Lock()
_expected_original: str | None = None
_requested_reference: str | None = None
_exact_original_seen = False


@dataclass(frozen=True)
class FakeProviderSnapshot:
    request_count: int
    requested_reference: str | None
    exact_original_seen: bool


def reset_calls(*, expected_original: str | None = None) -> None:
    global _expected_original, _requested_reference, _exact_original_seen
    with calls_lock:
        calls.clear()
        _expected_original = expected_original
        _requested_reference = None
        _exact_original_seen = False


def provider_snapshot() -> FakeProviderSnapshot:
    with calls_lock:
        return FakeProviderSnapshot(
            request_count=len(calls),
            requested_reference=_requested_reference,
            exact_original_seen=_exact_original_seen,
        )


def _contains_exact_original(messages: object, expected: str) -> bool:
    if not isinstance(messages, list):
        return False
    return any(
        isinstance(message, dict)
        and message.get("role") == "tool"
        and message.get("content") == expected
        for message in messages
    )


def _tool_call_response(reference: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-retrieve",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_headroom_retrieve",
                            "type": "function",
                            "function": {
                                "name": "headroom_retrieve",
                                "arguments": json.dumps({"hash": reference}),
                            },
                        }
                    ],
                },
            }
        ],
    }


def _final_response() -> dict[str, Any]:
    return {
        "id": "chatcmpl-final",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "FAKE_PROVIDER_CONFIRMED_CCR_ORIGINAL",
                },
            }
        ],
    }


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        global _requested_reference, _exact_original_seen
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        with calls_lock:
            calls.append(body)
            call_number = len(calls)
            expected_original = _expected_original

        if call_number == 1:
            tools = body.get("tools", [])
            has_retrieve = any(
                tool.get("function", {}).get("name") == "headroom_retrieve"
                for tool in tools
                if isinstance(tool, dict)
            )
            marker = HASH_PATTERN.search(
                json.dumps(body.get("messages", []), ensure_ascii=False)
            )
            if not has_retrieve or marker is None:
                self.send_error(422, "CCR tool or marker missing")
                return
            reference = marker.group(1)
            with calls_lock:
                _requested_reference = reference
            payload = _tool_call_response(reference)
        else:
            exact_original_seen = (
                _contains_exact_original(body.get("messages"), expected_original)
                if expected_original is not None
                else "CCR_ORIGINAL_FACT_7391"
                in json.dumps(body.get("messages", []), ensure_ascii=False)
            )
            with calls_lock:
                _exact_original_seen = exact_original_seen
            if not exact_original_seen:
                self.send_error(422, "retrieved original missing")
                return
            payload = _final_response()

        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return

"""Deterministic tests for the fake Headroom Proxy upstream."""

from io import BytesIO
import json

from tests.integration.fake_openai_provider import (
    FakeOpenAIHandler,
    provider_snapshot,
    reset_calls,
)


class RecordingHandler(FakeOpenAIHandler):
    status_code: int

    def send_response(self, code: int, message: str | None = None) -> None:
        del message
        self.status_code = code

    def send_header(self, keyword: str, value: str) -> None:
        del keyword, value

    def end_headers(self) -> None:
        return

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        del message, explain
        self.status_code = code


def _request(body: dict[str, object]) -> tuple[int, dict[str, object] | None]:
    encoded = json.dumps(body).encode("utf-8")
    handler = RecordingHandler.__new__(RecordingHandler)
    handler.headers = {"content-length": str(len(encoded))}
    handler.rfile = BytesIO(encoded)
    handler.wfile = BytesIO()
    handler.do_POST()
    payload = handler.wfile.getvalue()
    return (
        handler.status_code,
        json.loads(payload) if payload else None,
    )


def test_fake_provider_requests_retrieval_then_requires_exact_original() -> None:
    original = '{"record":"CCR_ORIGINAL_FACT_7391","value":"byte exact"}'
    first_request = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "tool",
                "content": "[compressed; retrieve: hash=abcdef123456]",
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "headroom_retrieve"},
            }
        ],
    }

    reset_calls(expected_original=original)
    first_status, first = _request(first_request)
    assert first_status == 200
    assert first is not None
    function = first["choices"][0]["message"]["tool_calls"][0][
            "function"
    ]
    assert function["name"] == "headroom_retrieve"
    assert "abcdef123456" in function["arguments"]

    second_status, second = _request(
        {
            "model": "gpt-4o",
            "messages": [
                *first_request["messages"],
                first["choices"][0]["message"],
                {
                    "role": "tool",
                    "tool_call_id": "call_headroom_retrieve",
                    "content": original,
                },
            ],
            "tools": first_request["tools"],
        }
    )
    assert second_status == 200
    assert second is not None
    assert second["choices"][0]["message"]["content"] == (
        "FAKE_PROVIDER_CONFIRMED_CCR_ORIGINAL"
    )
    snapshot = provider_snapshot()
    assert snapshot.request_count == 2
    assert snapshot.requested_reference == "abcdef123456"
    assert snapshot.exact_original_seen is True

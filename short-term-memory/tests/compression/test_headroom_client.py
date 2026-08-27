from pathlib import Path
import json
import logging
import tomllib

import httpx
import pytest

from short_term_memory.compression.headroom_client import HeadroomHttpClient
from short_term_memory.compression.telemetry import InMemoryHeadroomTelemetry
from short_term_memory.models import (
    HeadroomCompressionStatus,
    HeadroomFailureReason,
)


ROOT = Path(__file__).resolve().parents[2]
MESSAGES = (
    {"role": "user", "content": "当前目标是完成 Redis session"},
    {"role": "assistant", "content": "journals 保存完整原文"},
)


def test_package_does_not_install_headroom_python_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert all(
        not item.casefold().startswith("headroom-ai") for item in dependencies
    )


def test_package_source_does_not_import_headroom_package() -> None:
    offenders = []
    for path in (ROOT / "src" / "short_term_memory").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from headroom" in text or "import headroom" in text:
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []


def test_package_source_recall_goes_through_http_not_headroom_backend() -> None:
    """The project drives recall via the HTTP retrieve endpoint, never by reading
    Headroom's CCR backend directly (no sqlite file / home dir access)."""
    offenders = []
    for path in (ROOT / "src" / "short_term_memory").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Accessing Headroom's internal on-disk CCR store would violate the boundary.
        if "ccr_store.db" in text or "~/.headroom" in text or ".headroom/ccr" in text:
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []


def make_http_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_client_calls_public_compress_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "messages": [{"role": "user", "content": "压缩后的目标"}],
                "tokens_before": 100,
                "tokens_after": 40,
                "tokens_saved": 60,
                "compression_ratio": 0.4,
                "transforms_applied": ["router:text:0.40"],
            },
        )

    telemetry = InMemoryHeadroomTelemetry()
    client = HeadroomHttpClient(
        service_url="http://headroom:8787",
        environment="production",
        timeout_seconds=300,
        telemetry=telemetry,
        http_client=make_http_client(handler),
    )

    result = client.compress(MESSAGES, model="gpt-4o", correlation_id="job-1")

    assert requests[0].url == httpx.URL("http://headroom:8787/v1/compress")
    assert json.loads(requests[0].content) == {
        "messages": list(MESSAGES),
        "model": "gpt-4o",
    }
    assert result.status is HeadroomCompressionStatus.SUCCESS
    assert result.messages == ({"role": "user", "content": "压缩后的目标"},)
    assert result.compression_applied is True
    assert result.tokens_before == 100
    assert result.tokens_after == 40
    assert result.tokens_saved == 60
    assert result.transforms_applied == ("router:text:0.40",)
    assert telemetry.snapshot().success_count == 1
    assert telemetry.snapshot().compression_ratios == (2.5,)


def test_background_compression_sends_scope_headers_and_no_router_config() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "messages": list(MESSAGES),
                "tokens_before": 100,
                "tokens_after": 40,
                "tokens_saved": 60,
                "compression_ratio": 0.4,
                "transforms_applied": ["router:text:0.40"],
            },
        )

    client = HeadroomHttpClient(
        service_url="http://headroom:8787",
        environment="production",
        timeout_seconds=30,
        http_client=make_http_client(handler),
    )
    headers = {
        "x-headroom-user-id": "opaque-user",
        "x-headroom-session-id": "opaque-session",
        "x-headroom-project-id": "opaque-workspace",
    }

    client.compress(MESSAGES, model="gpt-4o", scope_headers=headers)

    assert {name: requests[0].headers[name] for name in headers} == headers
    assert json.loads(requests[0].content) == {
        "messages": list(MESSAGES),
        "model": "gpt-4o",
    }


def test_http_noop_is_a_valid_success() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": list(MESSAGES),
                "tokens_before": 50,
                "tokens_after": 50,
                "tokens_saved": 0,
                "compression_ratio": 1.0,
                "transforms_applied": ["router:noop"],
            },
        )

    telemetry = InMemoryHeadroomTelemetry()
    client = HeadroomHttpClient(
        service_url="http://headroom:8787",
        environment="production",
        timeout_seconds=300,
        telemetry=telemetry,
        http_client=make_http_client(handler),
    )

    result = client.compress(MESSAGES, model="gpt-4o")

    assert result.status is HeadroomCompressionStatus.SUCCESS
    assert result.compression_applied is False
    assert result.messages == MESSAGES
    assert telemetry.snapshot().noop_count == 1


def test_http_client_preserves_standard_message_fields_without_parsing_ccr() -> None:
    marker = "[500 items compressed to 15. Retrieve more: hash=abc_123]"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_search",
                        "content": marker,
                    }
                ],
                "tokens_before": 500,
                "tokens_after": 15,
                "tokens_saved": 485,
                "compression_ratio": 0.03,
                "transforms_applied": ["smart_crusher"],
                "ccr_hashes": ["abc_123"],
            },
        )

    client = HeadroomHttpClient(
        service_url="http://headroom:8787",
        environment="production",
        timeout_seconds=300,
        http_client=make_http_client(handler),
    )

    result = client.compress(MESSAGES, model="gpt-4o")

    assert result.messages == (
        {
            "role": "tool",
            "tool_call_id": "call_search",
            "content": marker,
        },
    )
    assert not hasattr(result, "recall_references")


@pytest.mark.parametrize(
    ("environment", "expected_fallback"),
    (("development", True), ("production", False)),
)
@pytest.mark.parametrize(
    ("failure_case", "expected_reason"),
    (
        ("connect", HeadroomFailureReason.SERVICE_UNAVAILABLE),
        ("timeout", HeadroomFailureReason.TIMEOUT),
        ("status", HeadroomFailureReason.HTTP_ERROR),
        ("json", HeadroomFailureReason.INVALID_RESPONSE),
        ("fields", HeadroomFailureReason.INVALID_RESPONSE),
    ),
)
def test_http_failures_are_private_and_environment_aware(
    failure_case: str,
    expected_reason: HeadroomFailureReason,
    environment: str,
    expected_fallback: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure_case == "connect":
            raise httpx.ConnectError("private endpoint", request=request)
        if failure_case == "timeout":
            raise httpx.ReadTimeout("private timeout", request=request)
        if failure_case == "status":
            return httpx.Response(503, text="private response")
        if failure_case == "json":
            return httpx.Response(200, content=b"not-json")
        return httpx.Response(200, json={"messages": "private invalid fields"})

    telemetry = InMemoryHeadroomTelemetry()
    client = HeadroomHttpClient(
        service_url="http://headroom:8787",
        environment=environment,
        timeout_seconds=300,
        telemetry=telemetry,
        http_client=make_http_client(handler),
    )

    with caplog.at_level(logging.WARNING):
        result = client.compress(
            MESSAGES, model="gpt-4o", correlation_id="job-1"
        )

    assert result.status is HeadroomCompressionStatus.FAILED
    assert result.failure_reason is expected_reason
    assert result.fallback_used is expected_fallback
    assert result.messages == (MESSAGES if expected_fallback else ())
    assert "private endpoint" not in caplog.text
    assert "private timeout" not in caplog.text
    assert "private response" not in caplog.text
    assert "private invalid fields" not in caplog.text
    assert "job-1" in caplog.text
    assert telemetry.snapshot().failure_count == 1
    assert telemetry.snapshot().fallback_count == int(expected_fallback)


def test_development_without_url_falls_back_without_http_request() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    telemetry = InMemoryHeadroomTelemetry()
    client = HeadroomHttpClient(
        service_url=None,
        environment="development",
        timeout_seconds=300,
        telemetry=telemetry,
        http_client=make_http_client(handler),
    )

    result = client.compress(MESSAGES, model="gpt-4o")

    assert calls == 0
    assert result.status is HeadroomCompressionStatus.FAILED
    assert result.failure_reason is HeadroomFailureReason.SERVICE_UNAVAILABLE
    assert result.fallback_used is True
    assert result.messages == MESSAGES


@pytest.mark.parametrize(
    "payload",
    (
        {
            "messages": [{"role": "user", "content": "compressed"}],
            "tokens_before": True,
            "tokens_after": 1,
            "tokens_saved": 0,
            "compression_ratio": 1.0,
            "transforms_applied": [],
        },
        {
            "messages": [{"role": "user", "content": "compressed"}],
            "tokens_before": 10,
            "tokens_after": 4,
            "tokens_saved": 5,
            "compression_ratio": 0.4,
            "transforms_applied": [],
        },
        {
            "messages": [{"role": None, "content": "compressed"}],
            "tokens_before": 10,
            "tokens_after": 4,
            "tokens_saved": 6,
            "compression_ratio": 0.4,
            "transforms_applied": [],
        },
    ),
)
def test_invalid_public_response_fields_are_rejected(payload: dict) -> None:
    client = HeadroomHttpClient(
        service_url="http://headroom:8787",
        environment="production",
        timeout_seconds=300,
        http_client=make_http_client(
            lambda _: httpx.Response(200, json=payload)
        ),
    )

    result = client.compress(MESSAGES, model="gpt-4o")

    assert result.status is HeadroomCompressionStatus.FAILED
    assert result.failure_reason is HeadroomFailureReason.INVALID_RESPONSE
    assert result.messages == ()

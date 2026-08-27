"""Opt-in no-cost test of official Headroom Proxy CCR continuation."""

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from threading import Thread
import time
from typing import Iterator

import httpx
import pytest

from tests.integration.fake_openai_provider import (
    FakeOpenAIHandler,
    HASH_PATTERN,
    calls,
    calls_lock,
    reset_calls,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("SHORT_TERM_MEMORY_RUN_HEADROOM_PROXY_CCR") != "1",
    reason=(
        "set SHORT_TERM_MEMORY_RUN_HEADROOM_PROXY_CCR=1 for real Headroom Proxy CCR test"
    ),
)

_SCOPE_HEADERS = {
    "authorization": "Bearer fake-key",
    "x-headroom-user-id": "dream-v1-test-user",
    "x-headroom-session-id": "dream-v1-test-session",
    "x-headroom-project-id": "dream-v1-test-workspace",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _fake_provider(port: int, expected_original: str) -> Iterator[None]:
    reset_calls(expected_original=expected_original)
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeOpenAIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _headroom_proxy(
    binary: str,
    proxy_port: int,
    fake_port: int,
) -> Iterator[str]:
    environment = os.environ.copy()
    environment["HEADROOM_CCR_TTL_SECONDS"] = "43200"
    environment["HEADROOM_TELEMETRY"] = "off"
    command = [
        binary,
        "proxy",
        "--host",
        "127.0.0.1",
        "--port",
        str(proxy_port),
        "--mode",
        "token",
        "--openai-api-url",
        f"http://127.0.0.1:{fake_port}",
        "--no-rate-limit",
        "--no-telemetry",
    ]
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{proxy_port}"
    try:
        deadline = time.monotonic() + 180
        last_error = "proxy did not start"
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise RuntimeError(f"Headroom exited early: {output[-2000:]}")
            try:
                response = httpx.get(f"{base_url}/livez", timeout=2)
                if response.status_code == 200:
                    ready = True
                    break
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            time.sleep(0.5)
        if not ready:
            raise RuntimeError(f"Headroom not ready: {last_error}")
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _tool_output_messages() -> list[dict[str, object]]:
    results = [
        {
            "id": index,
            "status": "ok",
            "detail": "repeated detail for deterministic compression",
        }
        for index in range(500)
    ]
    results.append(
        {
            "id": 7391,
            "status": "critical",
            "detail": "CCR_ORIGINAL_FACT_7391",
        }
    )
    return [
        {"role": "user", "content": "Find the recovery record"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_search",
                    "type": "function",
                    "function": {
                        "name": "search_records",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_search",
            "content": json.dumps({"results": results}),
        },
    ]


def test_real_proxy_compresses_and_transparently_resolves_ccr() -> None:
    binary = os.environ.get("SHORT_TERM_MEMORY_HEADROOM_BINARY") or shutil.which(
        "headroom"
    )
    if binary is None or not Path(binary).is_file():
        pytest.skip("Headroom binary is not installed")
    fake_port = _free_port()
    proxy_port = _free_port()

    original_messages = _tool_output_messages()
    expected_original = str(original_messages[-1]["content"])
    with _fake_provider(fake_port, expected_original), _headroom_proxy(
        binary, proxy_port, fake_port
    ) as base_url:
        compression_response = httpx.post(
            f"{base_url}/v1/compress",
            headers=_SCOPE_HEADERS,
            json={
                "model": "gpt-4o",
                "messages": original_messages,
            },
            timeout=300,
        )
        assert compression_response.status_code == 200, (
            compression_response.text
        )
        compression = compression_response.json()
        assert compression["tokens_after"] < compression["tokens_before"]
        proxy_response = httpx.post(
            f"{base_url}/v1/chat/completions",
            headers=_SCOPE_HEADERS,
            json={
                "model": "gpt-4o",
                "stream": False,
                "messages": compression["messages"],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "search_records",
                            "description": "Search recovery records",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                            },
                        },
                    }
                ],
            },
            timeout=300,
        )
        with calls_lock:
            diagnostic_calls = list(calls)
        diagnostic = {"upstream_calls": len(diagnostic_calls)}
        if diagnostic_calls:
            first = diagnostic_calls[0]
            diagnostic["has_marker"] = bool(
                HASH_PATTERN.search(
                    json.dumps(first.get("messages", []), ensure_ascii=False)
                )
            )
            diagnostic["tool_names"] = [
                tool.get("function", {}).get("name")
                for tool in first.get("tools", [])
                if isinstance(tool, dict)
            ]
        assert proxy_response.status_code == 200, (
            proxy_response.text,
            diagnostic,
        )
        response = proxy_response.json()
        with calls_lock:
            recorded = list(calls)
        upstream_marker = (
            HASH_PATTERN.search(
                json.dumps(recorded[0].get("messages", []), ensure_ascii=False)
            )
            if recorded
            else None
        )
        continuation_diagnostic = {
            "upstream_calls": len(recorded),
            "reference_length": (
                len(upstream_marker.group(1))
                if upstream_marker is not None
                else None
            ),
            "second_has_original": bool(
                len(recorded) > 1
                and "CCR_ORIGINAL_FACT_7391"
                in json.dumps(recorded[1], ensure_ascii=False)
            ),
        }
        stats = httpx.get(
            f"{base_url}/v1/retrieve/stats", timeout=30
        ).raise_for_status().json()

    assert response["choices"][0]["message"]["content"] == (
        "FAKE_PROVIDER_CONFIRMED_CCR_ORIGINAL"
    ), continuation_diagnostic
    assert len(recorded) == 2
    assert "CCR_ORIGINAL_FACT_7391" in json.dumps(
        recorded[1], ensure_ascii=False
    )
    assert stats["store"]["default_ttl_seconds"] == 43_200

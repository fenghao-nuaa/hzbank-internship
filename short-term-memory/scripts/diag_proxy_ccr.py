"""Verify Headroom Proxy REALTIME CCR on the /v1/chat/completions path.

This is the key question: when a raw long conversation is sent straight to
`POST /v1/chat/completions` (no pre-compression via /v1/compress), does the
Proxy:
  1. compress the tool output and store the original in CCR?
  2. inject a `headroom_retrieve` tool into the upstream request?
  3. intercept the model's headroom_retrieve call and continue with the original?

It spins up a disposable fake OpenAI provider and a disposable Headroom proxy,
so nothing real (DeepSeek) is called and no API key is used.

Usage:
    uv run python scripts/diag_proxy_ccr.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from tests.integration.fake_openai_provider import (  # noqa: E402
    FakeOpenAIHandler,
    HASH_PATTERN,
    calls,
    calls_lock,
    reset_calls,
)

_SCOPE_HEADERS = {
    "authorization": "Bearer fake-key",
    "x-headroom-user-id": "dream-v1-diag-user",
    "x-headroom-session-id": "dream-v1-diag-session",
    "x-headroom-project-id": "dream-v1-diag-project",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _raw_tool_messages() -> list[dict[str, Any]]:
    results = [
        {"id": i, "status": "ok", "detail": "repeated detail for deterministic compression"}
        for i in range(500)
    ]
    results.append({"id": 7391, "status": "critical", "detail": "CCR_ORIGINAL_FACT_7391"})
    return [
        {"role": "user", "content": "Find the recovery record"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_search", "type": "function",
                 "function": {"name": "search_records", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_search", "content": json.dumps({"results": results})},
    ]


def main() -> None:
    binary = os.environ.get("SHORT_TERM_MEMORY_HEADROOM_BINARY") or shutil.which("headroom")
    if not binary:
        print("headroom binary not found")
        return

    fake_port = _free_port()
    proxy_port = _free_port()
    messages = _raw_tool_messages()
    expected_original = str(messages[-1]["content"])
    reset_calls(expected_original=expected_original)

    # Start fake provider.
    server = None
    thread = None
    try:
        server = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(
            ("127.0.0.1", fake_port), FakeOpenAIHandler
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    except Exception as exc:  # noqa: BLE001
        print(f"fake provider start error: {type(exc).__name__}: {exc}")
        return

    env = os.environ.copy()
    env["HEADROOM_CCR_TTL_SECONDS"] = "43200"
    env["HEADROOM_TELEMETRY"] = "off"
    log_path = Path(f"/tmp/headroom-diag-{proxy_port}.jsonl")
    cmd = [
        binary, "proxy",
        "--host", "127.0.0.1",
        "--port", str(proxy_port),
        "--mode", "token",
        "--openai-api-url", f"http://127.0.0.1:{fake_port}",
        "--no-rate-limit",
        "--no-telemetry",
        "--log-file", str(log_path),
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{proxy_port}"

    try:
        # Wait for proxy to come up.
        deadline = time.monotonic() + 120
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                print(f"proxy exited early: {out[-1500:]}")
                return
            try:
                if httpx.get(f"{base}/livez", timeout=2).status_code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        if not ready:
            print("proxy not ready in time")
            return

        print("=== Realtime /v1/chat/completions with RAW long tool output ===")
        resp = httpx.post(
            f"{base}/v1/chat/completions",
            headers=_SCOPE_HEADERS,
            json={
                "model": "gpt-4o",
                "stream": False,
                "messages": messages,
                "tools": [
                    {"type": "function",
                     "function": {"name": "search_records", "description": "Search recovery records",
                                  "parameters": {"type": "object", "properties": {}}}}
                ],
            },
            timeout=300,
        )
        print("proxy status:", resp.status_code)
        try:
            print("proxy response body:", resp.text[:800])
        except Exception:  # noqa: BLE001
            pass

        with calls_lock:
            recorded = list(calls)
        print(f"\nupstream_calls: {len(recorded)}")
        for i, call in enumerate(recorded):
            marker = HASH_PATTERN.search(json.dumps(call.get("messages", []), ensure_ascii=False))
            tool_names = [
                t.get("function", {}).get("name")
                for t in call.get("tools", [])
                if isinstance(t, dict)
            ]
            has_original = expected_original in json.dumps(call.get("messages", []), ensure_ascii=False)
            print(f"  call {i}: marker={bool(marker)} tools={tool_names} has_original={has_original}")

        try:
            body = resp.json()
            content = body.get("choices", [{}])[0].get("message", {}).get("content")
            print(f"\nfinal content: {content!r}")
        except Exception:  # noqa: BLE001
            print("response body:", resp.text[:500])

        try:
            stats = httpx.get(f"{base}/v1/retrieve/stats", timeout=30).json()
            print("\nCCR store:", json.dumps(stats.get("store", {}), indent=2)[:500])
        except Exception as exc:  # noqa: BLE001
            print(f"\nretrieve/stats error: {type(exc).__name__}: {exc}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
            proc.wait(timeout=5)
        # Dump proxy logs for diagnosis.
        if proc.stdout is not None:
            try:
                log = proc.stdout.read()
                if log:
                    print("\n=== PROXY STDOUT (last 2000 chars) ===")
                    print(log[-2000:])
            except Exception:  # noqa: BLE001
                pass
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=5)
        if log_path.exists():
            print(f"\n=== PROXY LOG ({log_path.name}) ===")
            lines = log_path.read_text().splitlines()
            for ln in lines[-40:]:
                print(ln[:400])
            log_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

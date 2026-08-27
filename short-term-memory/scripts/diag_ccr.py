"""Diagnose Headroom CCR: does /v1/compress store originals retrievable by hash?

Usage:
    uv run python scripts/diag_ccr.py [--service-url http://127.0.0.1:8787]

Steps:
  1. POST /v1/compress with a 500-item JSON tool output.
  2. Inspect whether the response marks CCR (ccr_hashes / marker in messages).
  3. If a marker hash is present, try the proxy's own retrieve path and the
     fake upstream to see whether the original is actually recoverable.
"""

from __future__ import annotations

import argparse
import json
import os

import httpx


SCOPE_HEADERS = {
    "authorization": "Bearer fake-key",
    "x-headroom-user-id": "dream-v1-diag-user",
    "x-headroom-session-id": "dream-v1-diag-session",
    "x-headroom-project-id": "dream-v1-diag-project",
}


def build_tool_output_messages() -> list[dict[str, object]]:
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
                {
                    "id": "call_search",
                    "type": "function",
                    "function": {"name": "search_records", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_search", "content": json.dumps({"results": results})},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-url", default=os.environ.get("HEADROOM_SERVICE_URL", "http://127.0.0.1:8787"))
    args = parser.parse_args()
    base = args.service_url.rstrip("/")

    messages = build_tool_output_messages()

    # 1. Plain /v1/compress (what short_term_memory's worker sends today).
    print("=== 1. POST /v1/compress (default, no config.mode) ===")
    resp = httpx.post(
        f"{base}/v1/compress",
        headers=SCOPE_HEADERS,
        json={"model": "gpt-4o", "messages": messages},
        timeout=300,
    )
    resp.raise_for_status()
    comp = resp.json()
    print("tokens_before:", comp.get("tokens_before"))
    print("tokens_after:", comp.get("tokens_after"))
    print("ccr_hashes:", comp.get("ccr_hashes"))
    print("transforms_applied:", comp.get("transforms_applied"))
    has_marker = "Retrieve more" in json.dumps(comp.get("messages", []), ensure_ascii=False)
    print("has_marker_in_messages:", has_marker)

    # 2. Same but with config.mode="ccr".
    print()
    print("=== 2. POST /v1/compress (config.mode=ccr) ===")
    resp2 = httpx.post(
        f"{base}/v1/compress",
        headers=SCOPE_HEADERS,
        json={"model": "gpt-4o", "messages": messages, "config": {"mode": "ccr"}},
        timeout=300,
    )
    resp2.raise_for_status()
    comp2 = resp2.json()
    print("ccr_hashes:", comp2.get("ccr_hashes"))
    print("transforms_applied:", comp2.get("transforms_applied"))
    has_marker2 = "Retrieve more" in json.dumps(comp2.get("messages", []), ensure_ascii=False)
    print("has_marker_in_messages:", has_marker2)

    # 3. If markers exist, try proxy's /v1/retrieve endpoints.
    print()
    print("=== 3. CCR store stats ===")
    try:
        stats = httpx.get(f"{base}/v1/retrieve/stats", timeout=30).json()
        print(json.dumps(stats, indent=2)[:800])
    except Exception as exc:  # noqa: BLE001
        print(f"retrieve/stats error: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

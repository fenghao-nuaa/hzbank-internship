"""Determine whether Headroom 0.34 /v1/compress can emit CCR markers.

This is the foundation question for application-driven CCR recall: if
/v1/compress cannot produce retrievable markers, the recall client has nothing
to recall. Tries every plausible config combination and checks:
  - does the response contain ccr_hashes?
  - do the returned messages embed a "Retrieve more: hash=..." marker?
  - if a marker exists, does POST /v1/retrieve return the original?

Usage:
    uv run python scripts/diag_compress_markers.py
"""

from __future__ import annotations

import json
import os
import re

import httpx

SCOPE_HEADERS = {
    "x-headroom-user-id": "dream-v1-marker-user",
    "x-headroom-session-id": "dream-v1-marker-session",
    "x-headroom-project-id": "dream-v1-marker-project",
}

MARKER_RE = re.compile(r"Retrieve more:.*?hash=([0-9a-fA-F]{12,128})")


def _big_tool_messages() -> list[dict]:
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
    base = os.environ.get("HEADROOM_SERVICE_URL", "http://127.0.0.1:8787").rstrip("/")
    messages = _big_tool_messages()

    configs = {
        "default (no config)": None,
        'config.mode="ccr"': {"mode": "ccr"},
        'config.mode="lossy_inline"': {"mode": "lossy_inline"},
        'config.mode="lossless_then_lossy"': {"mode": "lossless_then_lossy"},
        'enable_ccr_marker=true': {"enable_ccr_marker": True},
        'mode="ccr" + enable_ccr_marker': {"mode": "ccr", "enable_ccr_marker": True},
    }

    print(f"service: {base}\n")
    for label, config in configs.items():
        body: dict = {"model": "gpt-4o", "messages": messages}
        if config is not None:
            body["config"] = config
        try:
            resp = httpx.post(f"{base}/v1/compress", headers=SCOPE_HEADERS, json=body, timeout=300)
            if resp.status_code != 200:
                print(f"[{label}] HTTP {resp.status_code}: {resp.text[:100]}")
                continue
            data = resp.json()
            ccr_hashes = data.get("ccr_hashes", [])
            transforms = data.get("transforms_applied", [])
            text = json.dumps(data.get("messages", []), ensure_ascii=False)
            markers = MARKER_RE.findall(text)
            print(f"[{label}]")
            print(f"    tokens: {data.get('tokens_before')} -> {data.get('tokens_after')}")
            print(f"    transforms: {transforms}")
            print(f"    ccr_hashes: {ccr_hashes}")
            print(f"    markers_in_messages: {markers[:2]}")
            # If a marker exists, try to retrieve it.
            if markers:
                h = markers[0]
                try:
                    r = httpx.post(f"{base}/v1/retrieve", headers=SCOPE_HEADERS, json={"hash": h}, timeout=15)
                    ok = r.status_code == 200 and "original_content" in r.text
                    print(f"    retrieve({h}): HTTP {r.status_code}, original_recovered={ok}")
                except Exception as exc:  # noqa: BLE001
                    print(f"    retrieve error: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] ERROR: {type(exc).__name__}: {exc}")
        print()


if __name__ == "__main__":
    main()

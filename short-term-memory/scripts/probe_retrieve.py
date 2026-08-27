"""Probe Headroom CCR retrieve endpoints with real marker hashes.

Reads markers from a real session summary in Redis, then tries every plausible
retrieve endpoint shape to see which one returns the original content.

Usage:
    uv run python scripts/probe_retrieve.py --user-id u-001 --session-id s-002
"""

from __future__ import annotations

import argparse
import json
import os
import re

import httpx
import redis

SCOPE_HEADERS = {
    "x-headroom-user-id": "dream-v1-probe-user",
    "x-headroom-session-id": "dream-v1-probe-session",
    "x-headroom-project-id": "dream-v1-probe-project",
}

MARKER_PATTERN = re.compile(r"(?:Retrieve more:|Retrieve original:)[^\n]*hash=([0-9a-fA-F]{12,24})")


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


def _extract_hashes(envelope: dict) -> list[str]:
    hashes: list[str] = []
    for gen in envelope.get("compression_generations", []):
        text = json.dumps(gen.get("messages", []), ensure_ascii=False)
        for m in MARKER_PATTERN.finditer(text):
            hashes.append(m.group(1))
        # Also match bare "hash=..." patterns.
        for m in re.finditer(r"hash=([0-9a-fA-F]{12,24})", text):
            hashes.append(m.group(1))
    return list(dict.fromkeys(hashes))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--service-url", default=os.environ.get("HEADROOM_SERVICE_URL", "http://127.0.0.1:8787"))
    args = parser.parse_args()
    base = args.service_url.rstrip("/")

    # Read summary from Redis.
    client = redis.Redis.from_url(_redis_url(), decode_responses=True)
    key = f"dream:session:{args.user_id}:{args.session_id}:summary"
    raw = client.get(key)
    if not raw:
        print(f"no summary at {key}")
        return
    envelope = json.loads(raw)
    hashes = _extract_hashes(envelope)
    print(f"found {len(hashes)} markers in summary: {hashes}")

    if not hashes:
        print("no markers found — cannot probe retrieve")
        return

    h = hashes[0]

    # Try every plausible retrieve endpoint shape.
    attempts = [
        ("POST", f"{base}/v1/retrieve", {"hash": h}),
        ("POST", f"{base}/v1/retrieve", {"hash": h, "original": True}),
        ("POST", f"{base}/v1/retrieve/{h}", None),
        ("GET", f"{base}/v1/retrieve/{h}", None),
        ("GET", f"{base}/v1/retrieve?hash={h}", None),
        ("POST", f"{base}/v1/retrieve/original", {"hash": h}),
        ("POST", f"{base}/v1/ccr/retrieve", {"hash": h}),
        ("GET", f"{base}/v1/ccr/retrieve?hash={h}", None),
        ("POST", f"{base}/ccr/retrieve", {"hash": h}),
        ("POST", f"{base}/v1/retrieve/ccr", {"hash": h}),
    ]

    print(f"\n=== probing with hash {h} ===")
    for method, url, body in attempts:
        try:
            if method == "POST":
                resp = httpx.post(url, headers=SCOPE_HEADERS, json=body, timeout=15)
            else:
                resp = httpx.get(url, headers=SCOPE_HEADERS, timeout=15)
            snippet = resp.text[:150].replace("\n", " ")
            print(f"{method} {url}\n    -> {resp.status_code}: {snippet}")
            if resp.status_code == 200 and len(resp.text) > 10:
                print("    *** SUCCESS: retrieve endpoint works! ***")
        except Exception as exc:  # noqa: BLE001
            print(f"{method} {url}\n    -> ERROR {type(exc).__name__}: {exc}")

    # Also show retrieve stats to see the store.
    try:
        stats = httpx.get(f"{base}/v1/retrieve/stats", headers=SCOPE_HEADERS, timeout=15)
        print(f"\nretrieve/stats -> {stats.status_code}: {stats.text[:300]}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nretrieve/stats ERROR: {exc}")


if __name__ == "__main__":
    main()

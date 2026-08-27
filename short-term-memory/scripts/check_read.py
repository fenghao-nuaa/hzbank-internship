"""Check what the read endpoint returns for a session (compressed vs original)."""

from __future__ import annotations

import json
import os

import httpx

MEMORY_URL = os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8080")


def main() -> None:
    user_id = input("user_id (默认 u-001): ").strip() or "u-001"
    session_id = input("session_id: ").strip()
    if not session_id:
        print("需要 session_id")
        return

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{MEMORY_URL}/v1/memories/read",
            json={"user_id": user_id, "session_id": session_id},
        )
        print(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:500])
            return
        d = resp.json()
        messages = d.get("messages", [])
        print(f"read 返回 messages 条数: {len(messages)}")
        total = 0
        for i, m in enumerate(messages):
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(content)
                preview = content[:60]
            else:
                preview = f"(非字符串: {type(content).__name__})"
            print(f"  [{i}] role={m.get('role')} len={len(content) if isinstance(content, str) else '?'} 前60字={preview!r}")
        print(f"总字符数: {total}")
        print(f"ccr_markers: {d.get('ccr_markers')}")
        print(f"compression_segments: {d.get('memory', {}).get('compression_segments')}")


if __name__ == "__main__":
    main()

"""Test code-content CCR: does conversation-shaped code produce a marker
that can be recalled?

Earlier a single long assistant message of code produced no marker. This test
uses a realistic conversation shape (user asks, assistant replies with code) and
checks whether Headroom emits a marker and whether /v1/retrieve recovers it.

Usage:
    uv run python scripts/diag_code_marker.py
"""

from __future__ import annotations

import json
import os
import re
import time

import httpx

SCOPE = {
    "x-headroom-user-id": "dream-v1-code-user",
    "x-headroom-session-id": "dream-v1-code-session",
    "x-headroom-project-id": "dream-v1-code-project",
}

MARKER_RE = re.compile(r"Retrieve more:.*?hash=([0-9a-fA-F]{12,128})")
HASH_RE = re.compile(r"hash=([0-9a-fA-F]{12,128})")


def main() -> None:
    base = os.environ.get("HEADROOM_SERVICE_URL", "http://127.0.0.1:8787").rstrip("/")

    # A Python module with a unique anchor, big enough to be compressible.
    code = '''# CODE_ORIGINAL_ANCHOR_7391
import json
import time

class MemoryStore:
    """Redis-backed short-term memory store."""

    def __init__(self, host="localhost", port=6379, db=0):
        self.r = redis.Redis(host=host, port=port, db=db, decode_responses=True)

    def add(self, session_id, role, content):
        mid = self._gen_id(session_id, time.time())
        self.r.rpush(f"session:{session_id}:ids", mid)
        self.r.set(f"msg:{mid}", json.dumps({"role": role, "content": content}))
        return mid

    def _gen_id(self, session_id, ts):
        return hashlib.md5(f"{session_id}:{ts}".encode()).hexdigest()

    def recent(self, session_id, n=5):
        ids = self.r.lrange(f"session:{session_id}:ids", -n, -1)
        return [json.loads(self.r.get(f"msg:{mid}")) for mid in ids if mid]

    def recall(self, session_id, query, top_k=10):
        # multi-way recall placeholder
        return self.recent(session_id, top_k)


def compress_if_needed(r, session_id, threshold=20):
    total = r.llen(f"session:{session_id}:ids")
    if total <= threshold:
        return
    old = r.lrange(f"session:{session_id}:ids", 0, total // 2)
    summary = "\\n".join(r.get(f"msg:{mid}") or "" for mid in old)
    r.set(f"session:{session_id}:summary", summary)
    r.ltrim(f"session:{session_id}:ids", total // 2, -1)


def main():
    store = MemoryStore()
    store.add("s-1", "user", "hello")
    print(store.recent("s-1"))


if __name__ == "__main__":
    main()
'''

    # Repeat the module to make it clearly large enough.
    big_code = "\n\n# ===== module repeated =====\n\n".join(
        [code] * 8
    )

    messages = [
        {"role": "user", "content": "请写一个 Redis 短期记忆存储的 Python 类，包含写入、最近读取、召回方法"},
        {"role": "assistant", "content": big_code},
    ]

    print(f"service: {base}")
    print(f"code length: {len(big_code)} chars, ~{len(big_code)//4} tokens est")

    for label, config in [
        ("default (no config)", None),
        ('config.mode="ccr"', {"mode": "ccr"}),
    ]:
        body = {"model": "gpt-4o", "messages": messages}
        if config:
            body["config"] = config
        started = time.perf_counter()
        try:
            resp = httpx.post(f"{base}/v1/compress", headers=SCOPE, json=body, timeout=300)
            elapsed = (time.perf_counter() - started) * 1000
            if resp.status_code != 200:
                print(f"[{label}] HTTP {resp.status_code}: {resp.text[:150]}")
                continue
            data = resp.json()
            before = data.get("tokens_before")
            after = data.get("tokens_after")
            transforms = data.get("transforms_applied", [])
            out_text = json.dumps(data.get("messages", []), ensure_ascii=False)
            markers = MARKER_RE.findall(out_text)
            hashes = HASH_RE.findall(out_text)
            print(f"[{label}] {before}->{after} tokens, {elapsed:.0f}ms, transforms={transforms}")
            print(f"    markers={markers[:3]} hashes={hashes[:3]}")

            if markers:
                h = markers[0]
                r2 = httpx.post(f"{base}/v1/retrieve", headers=SCOPE, json={"hash": h}, timeout=15)
                ok = r2.status_code == 200 and "original_content" in r2.text
                has_anchor = "CODE_ORIGINAL_ANCHOR_7391" in r2.text
                print(f"    retrieve({h}): HTTP {r2.status_code}, recovered={ok}, has_anchor={has_anchor}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] ERROR: {type(exc).__name__}: {exc}")
        print()


if __name__ == "__main__":
    main()

"""Test whether Headroom 0.33 /v1/compress emits markers for TEXT (conversation) content.

Your real scenario is conversation text, not JSON tool output. Earlier evidence
(the summary markers like "[255 items compressed to 101 ... hash=...]") came from
conversation content. This test sends a large text message to /v1/compress and
checks whether a retrievable marker is emitted.

Usage:
    uv run python scripts/diag_text_marker.py
"""

from __future__ import annotations

import json
import os
import re

import httpx

SCOPE_HEADERS = {
    "x-headroom-user-id": "dream-v1-text-user",
    "x-headroom-session-id": "dream-v1-text-session",
    "x-headroom-project-id": "dream-v1-text-project",
}

MARKER_RE = re.compile(r"Retrieve more:.*?hash=([0-9a-fA-F]{12,128})")


def main() -> None:
    base = os.environ.get("HEADROOM_SERVICE_URL", "http://127.0.0.1:8787").rstrip("/")

    # A long conversation-like text block with a unique anchor.
    paragraph = (
        "我们在杭州银行信息技术部实习，做的是一个短期记忆系统的项目。"
        "这个系统用Redis存储用户和DeepSeek的聊天上下文，上下文太长时调用Headroom压缩，"
        "压缩结果存回Redis，下次提问时取回压缩记忆和本次问题判断是否超限。"
        "我们用了Redis的LIST、HASH、ZSET数据结构，TTL设置为43200秒。"
        "压缩采用sequence单调递增的方式，保证已经压缩的内容不会被再次压缩。"
        "CCR召回机制让DeepSeek在信息不足时能找回原文。"
        "TEXT_ORIGINAL_ANCHOR_7391 这是用于验证召回的唯一标记。"
    )
    # Repeat to make it large enough to trigger compression.
    text = "\n".join(f"{i}: {paragraph}" for i in range(40))

    messages = [
        {"role": "user", "content": "请记住这个项目的情况"},
        {"role": "assistant", "content": text},
    ]

    print(f"service: {base}")
    print(f"input tokens estimate: {len(text) // 4}")

    configs = {
        "default (no config)": None,
        'config.mode="ccr"': {"mode": "ccr"},
        'config.mode="lossy_inline"': {"mode": "lossy_inline"},
    }

    for label, config in configs.items():
        body: dict = {"model": "gpt-4o", "messages": messages}
        if config is not None:
            body["config"] = config
        try:
            resp = httpx.post(f"{base}/v1/compress", headers=SCOPE_HEADERS, json=body, timeout=300)
            if resp.status_code != 200:
                print(f"[{label}] HTTP {resp.status_code}: {resp.text[:150]}")
                continue
            data = resp.json()
            ccr_hashes = data.get("ccr_hashes", [])
            transforms = data.get("transforms_applied", [])
            text_out = json.dumps(data.get("messages", []), ensure_ascii=False)
            markers = MARKER_RE.findall(text_out)
            print(f"[{label}]")
            print(f"    tokens: {data.get('tokens_before')} -> {data.get('tokens_after')}")
            print(f"    transforms: {transforms}")
            print(f"    ccr_hashes: {ccr_hashes}")
            print(f"    markers_in_messages: {markers[:3]}")
            if markers:
                h = markers[0]
                try:
                    r = httpx.post(f"{base}/v1/retrieve", headers=SCOPE_HEADERS, json={"hash": h}, timeout=15)
                    ok = r.status_code == 200 and "original_content" in r.text
                    contains_anchor = "TEXT_ORIGINAL_ANCHOR_7391" in r.text
                    print(f"    retrieve({h}): HTTP {r.status_code}, recovered={ok}, has_anchor={contains_anchor}")
                except Exception as exc:  # noqa: BLE001
                    print(f"    retrieve error: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] ERROR: {type(exc).__name__}: {exc}")
        print()


if __name__ == "__main__":
    main()

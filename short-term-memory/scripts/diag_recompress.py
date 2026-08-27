"""实测方案A：把压缩结果再压缩一次，看 hash 指向哪、召回返回什么。

步骤:
1. 压缩原文 -> 结果A + marker_hash_A
2. 用结果A 作为输入再压缩 -> 结果B + marker_hash_B
3. 对比 hash_A vs hash_B
4. 分别召回 hash_A、hash_B，看返回的是原文还是压缩中间结果
"""

from __future__ import annotations

import json
import re
import time

import httpx

SCOPE = {
    "x-headroom-user-id": "dream-v1-recompress-user",
    "x-headroom-session-id": "dream-v1-recompress-session",
    "x-headroom-project-id": "dream-v1-recompress-project",
}
base = "http://127.0.0.1:8787"
MARKER = re.compile(r"Retrieve more:.*?hash=([0-9a-fA-F]{12,128})")


def compress(messages: list[dict], label: str) -> tuple[dict, str | None]:
    resp = httpx.post(
        f"{base}/v1/compress", headers=SCOPE, json={"model": "gpt-4o", "messages": messages}, timeout=300
    )
    resp.raise_for_status()
    data = resp.json()
    out = json.dumps(data.get("messages", []), ensure_ascii=False)
    markers = MARKER.findall(out)
    hash_value = markers[0] if markers else None
    print(f"{label}: tokens {data.get('tokens_before')}->{data.get('tokens_after')}")
    print(f"  ccr_hashes={data.get('ccr_hashes')} marker={hash_value}")
    return data, hash_value


def recall(hash_value: str, label: str) -> None:
    if not hash_value:
        print(f"{label}: 无 hash，跳过召回")
        return
    resp = httpx.post(f"{base}/v1/retrieve", headers=SCOPE, json={"hash": hash_value}, timeout=15)
    if resp.status_code != 200:
        print(f"{label}: retrieve HTTP {resp.status_code}")
        return
    body = resp.json()
    content = body.get("original_content", "")
    print(f"{label}: retrieve 返回 {len(content)} 字符")
    print(f"  前80字: {content[:80]!r}")
    # 检查返回内容里是否含 marker（说明是压缩中间结果而非原文）
    has_marker = bool(MARKER.search(content))
    print(f"  返回内容含 marker: {has_marker}")


def main() -> None:
    # 用项目里的 document.md（40KB），之前实测确定产生 marker (8abe7013...)。
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "memory_cases" / "document.md"
    text = path.read_text(encoding="utf-8")
    print(f"输入文档: {path.name} ({len(text)} 字符)")
    messages = [
        {"role": "user", "content": "请总结这份文档"},
        {"role": "assistant", "content": text},
    ]

    print("=== 1. 第一次压缩原文 ===")
    data_a, hash_a = compress(messages, "第一次")

    print("\n=== 2. 用结果A 再压缩 ===")
    result_a_messages = data_a.get("messages", [])
    data_b, hash_b = compress(result_a_messages, "第二次(输入=结果A)")

    print("\n=== 3. 对比 hash ===")
    print(f"hash_A = {hash_a}")
    print(f"hash_B = {hash_b}")
    print(f"hash 相同: {hash_a == hash_b}")

    print("\n=== 4. 召回对比 ===")
    recall(hash_a, "召回 hash_A")
    recall(hash_b, "召回 hash_B")


if __name__ == "__main__":
    main()

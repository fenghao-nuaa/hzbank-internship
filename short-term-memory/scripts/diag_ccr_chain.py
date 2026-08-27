"""实测: 二次压缩后, 召回 hash_B 返回什么? 能否通过 hash_B 追到 hash_A 再取回原文?

核心问题:
1. 第一次压缩原文 -> 结果A + marker_A(hash_A)
2. 二次压缩(输入=结果A) -> 结果B + marker_B(hash_B)
3. 召回 hash_B -> 返回什么? 是结果A(含marker_A) 还是原文?
4. 如果返回结果A(含marker_A), 里面能否看到 hash_A? 能否再用 hash_A 召回原文?
"""

from __future__ import annotations

import json
import re

import httpx

SCOPE = {
    "x-headroom-user-id": "dream-v1-chain-user",
    "x-headroom-session-id": "dream-v1-chain-session",
    "x-headroom-project-id": "dream-v1-chain-project",
}
base = "http://127.0.0.1:8787"
MARKER = re.compile(r"Retrieve more:.*?hash=([0-9a-fA-F]{12,128})")


def compress(messages: list[dict], label: str) -> tuple[dict, list[str]]:
    resp = httpx.post(
        f"{base}/v1/compress", headers=SCOPE, json={"model": "gpt-4o", "messages": messages}, timeout=300
    )
    resp.raise_for_status()
    data = resp.json()
    out = json.dumps(data.get("messages", []), ensure_ascii=False)
    markers = MARKER.findall(out)
    print(f"{label}: tokens {data.get('tokens_before')}->{data.get('tokens_after')}")
    print(f"  transforms={data.get('transforms_applied')}")
    print(f"  markers={markers}")
    return data, markers


def retrieve(hash_value: str, label: str) -> str | None:
    if not hash_value:
        print(f"{label}: 无 hash")
        return None
    resp = httpx.post(f"{base}/v1/retrieve", headers=SCOPE, json={"hash": hash_value}, timeout=15)
    if resp.status_code != 200:
        print(f"{label}: retrieve HTTP {resp.status_code} {resp.text[:100]}")
        return None
    body = resp.json()
    content = body.get("original_content", "")
    inner_markers = MARKER.findall(content)
    print(f"{label}: retrieve 返回 {len(content)} 字符, 内含 marker={inner_markers}")
    print(f"  前100字: {content[:100]!r}")
    return content


def main() -> None:
    # 用 document.md (40KB), 确定能压缩且可能产生 marker
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "memory_cases" / "document.md"
    text = path.read_text(encoding="utf-8")
    messages = [
        {"role": "user", "content": "请总结这份文档"},
        {"role": "assistant", "content": text},
    ]

    print("=== 1. 第一次压缩原文 ===")
    data_a, markers_a = compress(messages, "第一次")

    print("\n=== 2. 用结果A 再压缩 ===")
    result_a_messages = data_a.get("messages", [])
    data_b, markers_b = compress(result_a_messages, "第二次(输入=结果A)")

    print("\n=== 3. 召回 hash_B ===")
    hash_b = markers_b[0] if markers_b else None
    content_b = retrieve(hash_b, "召回 hash_B")

    print("\n=== 4. 如果 hash_B 返回内容含 marker_A, 尝试用 hash_A 召回原文 ===")
    if content_b:
        inner = MARKER.findall(content_b)
        for h in inner:
            print(f"  hash_B 内容里的 marker: {h}")
            retrieve(h, f"  召回嵌套 hash {h}")

    print("\n=== 5. 对比 hash_A 是否等于 hash_B ===")
    print(f"markers_A={markers_a}")
    print(f"markers_B={markers_b}")
    print(f"相同: {markers_a == markers_b}")


if __name__ == "__main__":
    main()

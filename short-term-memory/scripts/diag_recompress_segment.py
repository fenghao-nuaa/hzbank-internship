"""实测: 把 summary 里压缩段的 messages 再压缩一次, 看 Headroom 能否进一步缩小。

这验证"二次压缩压缩段"是否有效: tokens_before vs tokens_after。
"""

from __future__ import annotations

import json

import httpx
import redis


def main() -> None:
    client = redis.Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    raw = client.get("dream:session:u-003:s-003:summary")
    if not raw:
        print("summary 不存在")
        return
    d = json.loads(raw)
    gens = d.get("compression_generations", [])
    messages: list[dict] = []
    for g in gens:
        for m in g.get("messages", []):
            messages.append({"role": m.get("role"), "content": m.get("content")})
    print(f"压缩段 messages 总数: {len(messages)}")

    # 估算输入字符数
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    print(f"输入总字符数: {total_chars}")

    scope = {
        "x-headroom-user-id": "dream-v1-recompress-user",
        "x-headroom-session-id": "dream-v1-recompress-session",
        "x-headroom-project-id": "dream-v1-recompress-project",
    }
    resp = httpx.post(
        "http://127.0.0.1:8787/v1/compress",
        headers=scope,
        json={"model": "gpt-4o", "messages": messages},
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"二次压缩结果: tokens {data.get('tokens_before')} -> {data.get('tokens_after')}")
    print(f"transforms: {data.get('transforms_applied')}")
    print(f"压缩率: {data.get('compression_ratio')}")


if __name__ == "__main__":
    main()

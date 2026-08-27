"""查看 summary 里各 generation 的 messages 内容, 确认 document.md 原文是否在压缩段里。"""

from __future__ import annotations

import json

import redis


def main() -> None:
    client = redis.Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    raw = client.get("dream:session:u-003:s-003:summary")
    if not raw:
        print("summary 不存在")
        return
    d = json.loads(raw)
    gens = d.get("compression_generations", [])
    print(f"generation 数: {len(gens)}")
    for g in gens:
        msgs = g.get("messages", [])
        total = sum(len(str(m.get("content", ""))) for m in msgs)
        print(f"gen{g.get('generation')}: {len(msgs)}条消息, 总字符数={total}")
        for m in msgs:
            c = str(m.get("content", ""))
            print(f"  [{m.get('role')}] len={len(c)} 前60={c[:60]!r}")


if __name__ == "__main__":
    main()

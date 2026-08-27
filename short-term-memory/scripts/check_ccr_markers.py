"""检查 s-003 的 read 返回: ccr_markers 是否为空, messages 内容。"""

from __future__ import annotations

import json

import httpx


def main() -> None:
    resp = httpx.post(
        "http://127.0.0.1:8080/v1/memories/read",
        json={"user_id": "u-003", "session_id": "s-003"},
        timeout=30,
    )
    d = resp.json()
    print("ccr_markers:", d.get("ccr_markers"))
    messages = d.get("messages", [])
    print("messages 条数:", len(messages))
    for i, m in enumerate(messages):
        c = m.get("content", "")
        if isinstance(c, str):
            preview = c[:40]
        else:
            preview = f"(非字符串: {type(c).__name__})"
        print(f"  [{i}] role={m.get('role')} len={len(c) if isinstance(c, str) else '?'} 前40={preview!r}")


if __name__ == "__main__":
    main()

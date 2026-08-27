"""诊断 DeepSeek 空回答的真实原因。

步骤:
1. 直接调 read 接口, 看 s-010 返回的 messages 结构 (每条多长, 是否异常)
2. 绕过记忆服务, 直接用 proxy_url + messages 调 DeepSeek, 复现是否空回答
3. 打印 DeepSeek 原始响应 (非流式), 看是空还是报错

Usage:
    uv run python scripts/diag_empty_answer.py
"""

from __future__ import annotations

import json
import os
import time

import httpx

MEMORY_URL = os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8080")
USER_ID = "u-001"
SESSION_ID = "s-010"


def main() -> None:
    # 1. 调 read 接口
    print("=== 1. read 接口返回 ===")
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{MEMORY_URL}/v1/memories/read",
            json={"user_id": USER_ID, "session_id": SESSION_ID},
        )
        print(f"HTTP {resp.status_code}")
        body = resp.json()
        messages = body.get("messages", [])
        print(f"messages 条数: {len(messages)}")
        total_chars = 0
        for i, m in enumerate(messages):
            content = m.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            print(
                f"  [{i}] role={m.get('role')} type={type(content).__name__} "
                f"len={len(content) if isinstance(content, str) else '?'}"
            )
            if isinstance(content, str) and content:
                print(f"      前80字: {content[:80]!r}")
        print(f"messages 总字符数: {total_chars}")

        headroom = body.get("headroom", {})
        proxy_url = headroom.get("proxy_url")
        scope_headers = headroom.get("scope_headers", {})
        ccr_markers = body.get("ccr_markers", [])
        print(f"\nheadroom.proxy_url: {proxy_url}")
        print(f"ccr_markers: {ccr_markers}")

        # 2. 绕过记忆服务, 直接用 messages 调 DeepSeek (非流式)
        if not proxy_url or not messages:
            print("\n无 proxy_url 或 messages, 跳过 DeepSeek 调用")
            return
        print("\n=== 2. 直接用 read 返回的 messages 调 DeepSeek (非流式) ===")
        from openai import OpenAI

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            print("无 DEEPSEEK_API_KEY, 跳过")
            return
        deepseek = OpenAI(
            api_key=api_key, base_url=proxy_url, default_headers=scope_headers
        )
        started = time.perf_counter()
        try:
            completion = deepseek.chat.completions.create(
                model="deepseek-v4-flash", messages=messages, stream=False
            )
            elapsed = (time.perf_counter() - started) * 1000
            content = completion.choices[0].message.content
            print(f"耗时: {elapsed:.0f}ms")
            print(f"finish_reason: {completion.choices[0].finish_reason}")
            print(f"content: {content!r}")
            print(f"content 长度: {len(content) if content else 0}")
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.perf_counter() - started) * 1000
            print(f"耗时: {elapsed:.0f}ms")
            print(f"ERROR: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

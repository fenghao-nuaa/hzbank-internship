"""Interactive multi-turn DeepSeek chat backed by short-term memory.

This is a thin wrapper around :class:`short_term_memory.agent.AgentChatClient`.
All memory orchestration (write -> read -> model -> recall tool-call loop ->
write answer) now lives in the SDK so integrators get it without copying this
example.

Usage:
    export DEEPSEEK_API_KEY=...
    uv run python examples/chat_loop.py --user-id u-001 --session-id s-001

Then type questions one line at a time. Type `exit`, `quit`, or Ctrl-C to stop.
Commands:
    @file <path> [question]   send a file as an attachment, optionally ask
"""

from __future__ import annotations

import argparse
import asyncio
import os
import traceback


from short_term_memory import AgentChatClient


def _build_model_call(deepseek_api_key: str, print_fn):
    """Return an async model_call wired to DeepSeek through Headroom proxy.

    The returned callable matches AgentChatClient's contract:
    async fn(messages, model, proxy_url, scope_headers) -> {"content", "tool_calls"}
    """
    from openai import OpenAI

    client_holder: dict[str, OpenAI] = {}

    async def model_call(
        *, messages, model, proxy_url, scope_headers, tools=None, **_kwargs
    ):
        if proxy_url not in client_holder:
            client_holder[proxy_url] = OpenAI(
                api_key=deepseek_api_key,
                base_url=proxy_url,
                default_headers=scope_headers,
            )
        client = client_holder[proxy_url]
        print_fn("[思考] 已发送，DeepSeek 正在生成...")
        completion = client.chat.completions.create(
            model=model, messages=messages, tools=tools, stream=False
        )
        message = completion.choices[0].message
        content = message.content
        tool_calls = getattr(message, "tool_calls", None)
        tc_list = []
        if tool_calls:
            for tc in tool_calls:
                print_fn(
                    f"[tool] DeepSeek 调用工具 {tc.function.name}({tc.function.arguments})"
                )
                tc_list.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                )
        return {"content": content, "tool_calls": tc_list}

    return model_call


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive multi-turn DeepSeek chat with short-term memory."
    )
    parser.add_argument(
        "--memory-api-url",
        default=os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8080"),
        help="Short-term memory API base URL.",
    )
    parser.add_argument("--user-id", required=True, help="Memory user ID.")
    parser.add_argument("--session-id", required=True, help="Memory session ID.")
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        help="DeepSeek model name.",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        default=None,
        help=(
            "Read a file (document/code/etc.) and send its content as the first "
            "user message so it enters the memory store."
        ),
    )
    args = parser.parse_args()

    deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not deepseek_api_key:
        parser.error("DEEPSEEK_API_KEY is required")

    auth_token = os.environ.get("MEMORY_API_AUTH_TOKEN")
    model_call = _build_model_call(deepseek_api_key, print)

    print("=" * 60)
    print(f"Interactive chat | user={args.user_id} session={args.session_id}")
    print("Type a message and press Enter. `exit` / `quit` to stop.")
    print("=" * 60)

    client = AgentChatClient(
        memory_api_url=args.memory_api_url,
        model_call=model_call,
        auth_token=auth_token,
    )

    async def run() -> None:
        # Preview the historical session's compressed view on open.
        try:
            preview = await client.preview_history(args.user_id, args.session_id)
            print(AgentChatClient.format_history_preview(preview))
        except Exception as exc:  # noqa: BLE001
            print(f"[history] 预览历史失败: {type(exc).__name__}: {exc}")

        # If --file is given, send it as the first user turn.
        if args.file:
            from pathlib import Path

            path = Path(args.file)
            if not path.is_file():
                print(f"[error] file not found: {args.file}")
                return
            content = path.read_text(encoding="utf-8", errors="replace")
            print(f"[file] 已读取 {path.name} ({len(content)} 字符)，作为首条消息发送...")
            try:
                answer = await client.turn(
                    args.user_id, args.session_id, content, model=args.model
                )
                print(f"\nDeepSeek> {answer}")
            except Exception as exc:  # noqa: BLE001
                print(f"[error] 发送文件内容失败: {type(exc).__name__}: {exc}")
                traceback.print_exc()

        while True:
            try:
                prompt = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not prompt:
                continue
            if prompt.lower() in {"exit", "quit", "退出"}:
                break

            if prompt.startswith("@file "):
                rest = prompt[len("@file ") :].strip()
                parts = rest.split(None, 1)
                file_path = parts[0]
                question = parts[1] if len(parts) > 1 else "请阅读这份文件并介绍其内容"
                from pathlib import Path

                path = Path(file_path).expanduser()
                if not path.is_file():
                    print(f"[error] 文件不存在: {file_path}")
                    continue
                file_content = path.read_text(encoding="utf-8", errors="replace")
                print(f"[file] 已读取 {path.name} ({len(file_content)} 字符)，作为附件发送...")
                try:
                    await client.turn(
                        args.user_id, args.session_id, file_content, model=args.model
                    )
                    answer = await client.turn(
                        args.user_id, args.session_id, question, model=args.model
                    )
                    print(f"\nDeepSeek> {answer}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[error] 发送文件失败: {type(exc).__name__}: {exc}")
                    traceback.print_exc()
                continue

            try:
                answer = await client.turn(
                    args.user_id, args.session_id, prompt, model=args.model
                )
                print(f"\nDeepSeek> {answer}")
            except Exception as exc:  # noqa: BLE001
                print(f"[error] {type(exc).__name__}: {exc}")
                traceback.print_exc()

    try:
        asyncio.run(run())
    finally:
        print("再见！会话已保存在 memory 中。")


if __name__ == "__main__":
    main()

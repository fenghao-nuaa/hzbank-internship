"""Test which of the four content types (conversation/code/document/skill)
produce CCR markers when compressed by Headroom /v1/compress.

Usage:
    uv run python scripts/diag_content_markers.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "memory_cases"

MARKER_RE = re.compile(r"Retrieve more:.*?hash=([0-9a-fA-F]{12,128})")


def main() -> None:
    base = os.environ.get("HEADROOM_SERVICE_URL", "http://127.0.0.1:8787").rstrip("/")

    cases = [
        ("conversation", "conversation.txt"),
        ("code", "code.py"),
        ("document", "document.md"),
        ("skill", "SKILL.md"),
    ]

    print(f"service: {base}\n")
    for kind, filename in cases:
        path = FIXTURES / filename
        content = path.read_text(encoding="utf-8")
        messages = [
            {"role": "user", "content": f"请分析这个{kind}"},
            {"role": "assistant", "content": content},
        ]
        scope = {
            "x-headroom-user-id": "dream-v1-content-user",
            "x-headroom-session-id": f"dream-v1-{kind}-session",
            "x-headroom-project-id": "dream-v1-content-project",
        }
        try:
            resp = httpx.post(f"{base}/v1/compress", headers=scope, json={"model": "gpt-4o", "messages": messages}, timeout=300)
            if resp.status_code != 200:
                print(f"[{kind}] HTTP {resp.status_code}: {resp.text[:150]}")
                continue
            data = resp.json()
            before = data.get("tokens_before")
            after = data.get("tokens_after")
            transforms = data.get("transforms_applied", [])
            out_text = json.dumps(data.get("messages", []), ensure_ascii=False)
            markers = MARKER_RE.findall(out_text)
            ccr_hashes = data.get("ccr_hashes", [])
            print(f"[{kind}] {before} -> {after} tokens, transforms={transforms}")
            print(f"    markers={markers[:3]} ccr_hashes={ccr_hashes}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{kind}] ERROR: {type(exc).__name__}: {exc}")
        print()


if __name__ == "__main__":
    main()

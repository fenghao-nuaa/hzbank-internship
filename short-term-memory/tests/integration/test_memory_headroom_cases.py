"""Opt-in acceptance of real Headroom compression and CCR retrieval."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "memory_cases"
RUN_REAL_HEADROOM = (
    os.environ.get("SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING") == "1"
)

pytestmark = pytest.mark.skipif(
    not RUN_REAL_HEADROOM,
    reason=(
        "set SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING=1 for real Headroom "
        "fixture compression and CCR retrieval"
    ),
)

CASES = (
    ("conversation", "conversation.txt", "CONVERSATION_ORIGINAL_ANCHOR_7391"),
    ("code", "code.py", "CODE_ORIGINAL_ANCHOR_7391"),
    ("document", "document.md", "DOCUMENT_ORIGINAL_ANCHOR_7391"),
    ("skill", "SKILL.md", "SKILL_ORIGINAL_ANCHOR_7391"),
)
HASH_PATTERN = re.compile(r"hash=([0-9a-fA-F]{12,24})")


def _service_url() -> str:
    return os.environ.get(
        "HEADROOM_SERVICE_URL", "http://127.0.0.1:8787"
    ).rstrip("/")


def _scope_headers(kind: str) -> dict[str, str]:
    return {
        "x-headroom-user-id": "memory-cases-user",
        "x-headroom-session-id": f"memory-cases-{kind}",
        "x-headroom-project-id": "memory-cases-project",
    }


@pytest.mark.parametrize(("kind", "filename", "anchor"), CASES)
def test_headroom_compresses_and_retrieves_byte_identical_original(
    kind: str, filename: str, anchor: str
) -> None:
    original = (FIXTURES / filename).read_text(encoding="utf-8")
    original_digest = sha256(original.encode("utf-8")).digest()
    headers = _scope_headers(kind)

    compressed_response = httpx.post(
        f"{_service_url()}/v1/compress",
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": original}],
        },
        headers=headers,
        timeout=300,
    )
    compressed_response.raise_for_status()
    compressed = compressed_response.json()

    tokens_before = compressed["tokens_before"]
    tokens_after = compressed["tokens_after"]
    reported_ratio = compressed["compression_ratio"]
    assert tokens_before > 0
    assert 0 <= tokens_after < tokens_before
    assert 0 <= reported_ratio < 1
    assert reported_ratio == pytest.approx(tokens_after / tokens_before, abs=0.02)

    rendered = json.dumps(compressed["messages"], ensure_ascii=False)
    marker = HASH_PATTERN.search(rendered)
    assert marker is not None, "real Headroom response did not contain a CCR hash"
    retrieved_response = httpx.get(
        f"{_service_url()}/v1/retrieve/{marker.group(1)}",
        headers=headers,
        timeout=10,
    )
    retrieved_response.raise_for_status()
    retrieved = retrieved_response.json()["original_content"]

    assert isinstance(retrieved, str)
    assert retrieved.count(anchor) == 1
    assert retrieved.encode("utf-8") == original.encode("utf-8")
    assert sha256(retrieved.encode("utf-8")).digest() == original_digest

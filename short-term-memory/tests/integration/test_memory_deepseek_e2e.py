"""Opt-in, cost-bearing three-turn DeepSeek-through-Headroom acceptance."""

import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "memory_cases"
RUN_REAL_DEEPSEEK = os.environ.get("SHORT_TERM_MEMORY_RUN_DEEPSEEK_E2E") == "1"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()

pytestmark = pytest.mark.skipif(
    not RUN_REAL_DEEPSEEK or not DEEPSEEK_API_KEY,
    reason=(
        "set SHORT_TERM_MEMORY_RUN_DEEPSEEK_E2E=1 and DEEPSEEK_API_KEY for "
        "the cost-bearing real DeepSeek three-turn test"
    ),
)

MODEL = "deepseek-v4-flash"
USER_ID = "memory-deepseek-e2e-user"
SESSION_ID = f"memory-deepseek-e2e-{uuid4().hex}"
EXPECTED_GENERATION_INPUTS = (
    list(range(1, 101)),
    list(range(101, 181)),
    list(range(181, 241)),
)
EXPECTED_ANCHORS = (
    "CONVERSATION_ORIGINAL_ANCHOR_7391",
    "DOCUMENT_ORIGINAL_ANCHOR_7391",
    "SKILL_ORIGINAL_ANCHOR_7391",
)


def _memory_headers() -> dict[str, str]:
    token = os.environ.get("MEMORY_API_AUTH_TOKEN", "").strip()
    return {"authorization": f"Bearer {token}"} if token else {}


def _post_memory(
    client: httpx.Client, path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = client.post(path, json=payload)
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        pytest.fail(f"memory API request failed: {type(exc).__name__}", pytrace=False)
    assert isinstance(result, dict)
    return result


def _event(
    sequence: int, role: str, content_type: str, content: str
) -> dict[str, Any]:
    return {
        "event_id": f"deepseek-e2e-{sequence}-{uuid4().hex}",
        "role": role,
        "content_type": content_type,
        "content": f"[ACCEPTANCE_SEQUENCE:{sequence}]\n{content}",
        "metadata": {"acceptance_sequence": str(sequence)},
    }


def _first_generation() -> list[dict[str, Any]]:
    fixture_cases = (
        ("conversation", "conversation.txt"),
        ("code", "code.py"),
        ("document", "document.md"),
        ("skill", "SKILL.md"),
    )
    events = [
        _event(
            index,
            "user",
            content_type,
            (FIXTURES / filename).read_text(encoding="utf-8"),
        )
        for index, (content_type, filename) in enumerate(fixture_cases, start=1)
    ]
    for sequence in range(5, 100):
        events.append(
            _event(
                sequence,
                "user",
                "conversation",
                "Deterministic background record for a contiguous compression "
                f"generation. Record number {sequence}. " * 8,
            )
        )
    events.append(
        _event(
            100,
            "user",
            "conversation",
            "Return only the sealed recovery phrase from the original conversation.",
        )
    )
    return events


def _later_generation(
    start: int, through: int, previous_answer: str, target: str
) -> list[dict[str, Any]]:
    events = [
        _event(start, "assistant", "conversation", previous_answer)
    ]
    for sequence in range(start + 1, through):
        events.append(
            _event(
                sequence,
                "user",
                "conversation",
                "Deterministic continuation record that preserves exact sequence "
                f"coverage. Record number {sequence}. " * 8,
            )
        )
    events.append(_event(through, "user", "conversation", target))
    return events


def _wait_for_compression(
    client: httpx.Client, through_sequence: int
) -> dict[str, Any]:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        memory = _post_memory(
            client,
            "/v1/memories/read",
            {"user_id": USER_ID, "session_id": SESSION_ID},
        )
        if memory["memory"]["compressed_through_sequence"] >= through_sequence:
            return memory
        time.sleep(0.5)
    pytest.fail("compression did not reach the required sequence", pytrace=False)


def _retrieval_count(stats: object) -> int:
    if isinstance(stats, dict):
        for name in ("retrievals", "retrieval_count", "total_retrievals"):
            value = stats.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        for value in stats.values():
            try:
                return _retrieval_count(value)
            except AssertionError:
                pass
    raise AssertionError("Headroom stats omitted a retrieval count")


def _headroom_stats() -> dict[str, Any]:
    service_url = os.environ.get(
        "HEADROOM_SERVICE_URL", "http://127.0.0.1:8787"
    ).rstrip("/")
    try:
        response = httpx.get(f"{service_url}/v1/retrieve/stats", timeout=30)
        response.raise_for_status()
        stats = response.json()
    except Exception as exc:
        pytest.fail(f"Headroom stats request failed: {type(exc).__name__}", pytrace=False)
    assert isinstance(stats, dict)
    return stats


def _ask_deepseek(memory: dict[str, Any], expected_anchor: str) -> str:
    try:
        from openai import OpenAI

        headroom = memory["headroom"]
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=headroom["proxy_url"],
            default_headers=headroom["scope_headers"],
        )
        response = client.chat.completions.create(
            model=MODEL,
            messages=memory["messages"],
        )
    except Exception as exc:
        pytest.fail(f"DeepSeek request failed: {type(exc).__name__}", pytrace=False)
    answer = response.choices[0].message.content
    assert response.model == MODEL
    assert isinstance(answer, str)
    assert expected_anchor in answer
    return answer


def test_real_deepseek_three_turns_retrieve_three_original_generations() -> None:
    memory_url = os.environ.get(
        "MEMORY_API_URL", "http://127.0.0.1:8080"
    ).rstrip("/")
    captured_generation_inputs: list[list[int]] = []
    stats_before = _headroom_stats()

    with httpx.Client(
        base_url=memory_url, headers=_memory_headers(), timeout=30
    ) as memory_client:
        batches = [_first_generation()]
        previous_answer = ""
        targets = (
            "Return only the sealed recovery phrase from the original document.",
            "Return only the recovery phrase defined by the original skill.",
        )
        for turn, expected_anchor in enumerate(EXPECTED_ANCHORS):
            if turn:
                start = EXPECTED_GENERATION_INPUTS[turn][0]
                through = EXPECTED_GENERATION_INPUTS[turn][-1]
                batches.append(
                    _later_generation(
                        start, through, previous_answer, targets[turn - 1]
                    )
                )
            batch = batches[turn]
            written = _post_memory(
                memory_client,
                "/v1/memories/write",
                {
                    "user_id": USER_ID,
                    "session_id": SESSION_ID,
                    "events": batch,
                },
            )
            assert written["accepted"] is True
            observed_range = list(
                range(written["sequence_from"], written["sequence_through"] + 1)
            )
            captured_generation_inputs.append(observed_range)
            memory = _wait_for_compression(
                memory_client, EXPECTED_GENERATION_INPUTS[turn][-1]
            )
            previous_answer = _ask_deepseek(memory, expected_anchor)

        _post_memory(
            memory_client,
            "/v1/memories/write",
            {
                "user_id": USER_ID,
                "session_id": SESSION_ID,
                "events": [
                    _event(241, "assistant", "conversation", previous_answer)
                ],
            },
        )

    stats_after = _headroom_stats()
    assert _retrieval_count(stats_after) > _retrieval_count(stats_before)
    assert captured_generation_inputs == list(EXPECTED_GENERATION_INPUTS)

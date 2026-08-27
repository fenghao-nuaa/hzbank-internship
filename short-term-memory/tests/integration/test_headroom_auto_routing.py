"""Opt-in evidence that official Headroom owns compressor selection."""

import json
import os
import time

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING") != "1",
    reason=(
        "set SHORT_TERM_MEMORY_RUN_HEADROOM_AUTO_ROUTING=1 for ContentRouter tests"
    ),
)


def service_url() -> str:
    value = os.environ.get(
        "HEADROOM_SERVICE_URL", "http://127.0.0.1:8787"
    )
    return value.rstrip("/")


@pytest.fixture(scope="module", autouse=True)
def wait_for_kompress_warmup() -> None:
    deadline = time.monotonic() + 180
    last_status: object = None
    warmup_attempted = False
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{service_url()}/health", timeout=5)
            health = response.json()
            last_status = health.get("checks", {}).get("kompress")
            if isinstance(last_status, dict) and last_status.get("ready") is True:
                return
            if not warmup_attempted:
                warmup_attempted = True
                httpx.post(
                    f"{service_url()}/v1/compress",
                    json={
                        "model": "gpt-4o",
                        "messages": [
                            {
                                "role": "assistant",
                                "content": (
                                    "Headroom lazy Kompress warm-up text. " * 500
                                ),
                            }
                        ],
                    },
                    timeout=300,
                ).raise_for_status()
        except (httpx.HTTPError, ValueError):
            last_status = "unreachable"
        time.sleep(1)
    pytest.fail(f"Kompress did not become ready: {last_status!r}")


def large_json_tool_message() -> tuple[list[dict[str, object]], str]:
    anchor = "JSON_ROUTE_ANCHOR_7391"
    content = json.dumps(
        {
            "results": [
                {
                    "id": index,
                    "status": "ok",
                    "detail": (
                        "repeated successful record for routing verification"
                    ),
                }
                for index in range(500)
            ]
            + [{"id": 7391, "status": "error", "detail": anchor}]
        }
    )
    return (
        [
            {"role": "user", "content": f"Find {anchor}"},
            {
                "role": "tool",
                "tool_call_id": "call_json",
                "content": content,
            },
        ],
        anchor,
    )


def long_plain_text_message() -> tuple[list[dict[str, object]], str]:
    anchor = "TEXT_ROUTE_ANCHOR_7391"
    paragraph = (
        "DREAM keeps Redis session context and journals preserve complete "
        "events. Headroom automatically selects a compressor without a "
        "DREAM hint. "
    )
    return (
        [{"role": "assistant", "content": anchor + "\n" + paragraph * 500}],
        anchor,
    )


def long_pytest_log_message() -> tuple[list[dict[str, object]], str]:
    anchor = "LOG_ROUTE_ANCHOR_7391"
    passed = "tests/test_memory.py::test_session PASSED\n" * 700
    log = (
        "===== test session starts =====\n"
        + passed
        + f"tests/test_ccr.py::test_restore FAILED\nAssertionError: {anchor}\n"
        + "===== 1 failed, 700 passed =====\n"
    )
    return (
        [{"role": "tool", "tool_call_id": "call_log", "content": log}],
        anchor,
    )


def long_search_result_message() -> tuple[list[dict[str, object]], str]:
    anchor = "SEARCH_ROUTE_ANCHOR_7391"
    lines = [
        f"src/module_{index}.py:{index + 1}:def repeated_handler_{index}(): pass"
        for index in range(1000)
    ]
    lines.append(f"src/auth.py:7391:def {anchor}(): pass")
    return (
        [
            {"role": "user", "content": f"Find {anchor}"},
            {
                "role": "tool",
                "tool_call_id": "call_search",
                "content": "\n".join(lines),
            },
        ],
        anchor,
    )


def long_unified_diff_message() -> tuple[list[dict[str, object]], str]:
    anchor = "DIFF_ROUTE_ANCHOR_7391"
    hunks = []
    for index in range(400):
        hunks.append(
            "\n".join(
                (
                    f"@@ -{index + 1},1 +{index + 1},1 @@",
                    f"-old_value_{index} = False",
                    f"+new_value_{index} = True",
                )
            )
        )
    hunks.append(
        "@@ -7391,1 +7391,1 @@\n-old = False\n+"
        + anchor
        + " = True"
    )
    diff = (
        "diff --git a/state.py b/state.py\n"
        "--- a/state.py\n"
        "+++ b/state.py\n"
        + "\n".join(hunks)
    )
    return (
        [{"role": "tool", "tool_call_id": "call_diff", "content": diff}],
        anchor,
    )


JSON_MESSAGES, JSON_ANCHOR = large_json_tool_message()
TEXT_MESSAGES, TEXT_ANCHOR = long_plain_text_message()
LOG_MESSAGES, LOG_ANCHOR = long_pytest_log_message()
SEARCH_MESSAGES, SEARCH_ANCHOR = long_search_result_message()
DIFF_MESSAGES, DIFF_ANCHOR = long_unified_diff_message()

CASES = (
    ("json", JSON_MESSAGES, JSON_ANCHOR, ("smart", "json")),
    ("text", TEXT_MESSAGES, TEXT_ANCHOR, ("text", "kompress")),
    ("log", LOG_MESSAGES, LOG_ANCHOR, ("log",)),
    ("search", SEARCH_MESSAGES, SEARCH_ANCHOR, ("search",)),
    ("diff", DIFF_MESSAGES, DIFF_ANCHOR, ("diff",)),
)


@pytest.mark.parametrize(
    ("name", "messages", "anchor", "route_fragments"), CASES
)
def test_headroom_selects_compressor_without_dream_hint(
    name: str,
    messages: list[dict[str, object]],
    anchor: str,
    route_fragments: tuple[str, ...],
) -> None:
    response = httpx.post(
        f"{service_url()}/v1/compress",
        json={"model": "gpt-4o", "messages": messages},
        timeout=300,
    ).raise_for_status().json()

    transforms = " ".join(response["transforms_applied"]).casefold()
    assert response["tokens_after"] < response["tokens_before"], (
        name,
        transforms,
    )
    assert any(fragment in transforms for fragment in route_fragments), (
        transforms
    )
    assert anchor in json.dumps(response["messages"], ensure_ascii=False)

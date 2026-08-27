from datetime import datetime, timedelta, timezone

import pytest

from short_term_memory.compression.session_memory_compact import (
    SessionMemoryCompactContext,
    build_post_compact_messages,
    find_coverage_index,
    materialize_session_memory_recovery_revision,
    try_session_memory_compaction,
)
from short_term_memory.compression.session_memory_prompt import EMPTY_SESSION_MEMORY
from short_term_memory.models import SessionCompressionMessage, SessionMemoryRevision
from tests.factories import memory_event


NOW = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)


def message(role: str, content="text", *, sequence: int, tokens: int = 1_000, **extra):
    return SessionCompressionMessage(
        role=role,
        content=content,
        stm_sequence_from=sequence,
        stm_sequence_through=sequence,
        test_tokens=tokens,
        **extra,
    )


def revision(*, through: int = 2, content: str = "populated session memory"):
    return SessionMemoryRevision(
        version=1,
        content=content,
        covered_through_sequence=through,
        token_count=10_000,
        updated_at=NOW.isoformat(),
    )


class FieldEstimator:
    def estimate(self, messages):
        return sum(int(item.get("test_tokens", 0)) for item in messages)


def context(**updates) -> SessionMemoryCompactContext:
    values = {
        "token_estimator": FieldEstimator(),
        "history_turns": 1,
        "auto_compact_threshold": 100_000,
        "clock": lambda: NOW,
    }
    values.update(updates)
    return SessionMemoryCompactContext(**values)


def ordinary_messages(count: int = 8, *, tokens: int = 2_000):
    return tuple(
        message(
            "user" if index % 2 else "assistant",
            sequence=index,
            tokens=tokens,
        )
        for index in range(1, count + 1)
    )


def test_materialize_l4_recovery_revision_needs_no_model_and_keeps_recent_tail():
    recovered = materialize_session_memory_recovery_revision(
        session_memory=revision(through=80, content="session memory facts"),
        recent_originals=(
            memory_event(sequence=79, event_id="old", content="old"),
            memory_event(sequence=80, event_id="covered", content="covered"),
            memory_event(sequence=81, event_id="tail", content="new tail"),
        ),
        now=NOW,
    )

    assert recovered.boundary.strategy == "session_memory"
    assert recovered.boundary.trigger == "reactive"
    assert recovered.boundary.covered_through_sequence == 80
    assert "session memory facts" in str(recovered.summary_message.content)
    assert [item.content for item in recovered.messages_to_keep] == ["new tail"]
    assert recovered.messages_to_keep[0].model_extra["stm_sequence_through"] == 81


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [None, revision(content=EMPTY_SESSION_MEMORY)])
async def test_l4_falls_back_when_memory_is_missing_or_empty(memory) -> None:
    assert await try_session_memory_compaction(
        messages=ordinary_messages(), session_memory=memory, context=context()
    ) is None


@pytest.mark.asyncio
async def test_in_progress_extraction_completing_is_used_with_claude_wait_limits() -> None:
    newer = revision(through=4, content="fresh memory")
    calls = []

    async def waiter():
        calls.append("waited")
        return newer

    result = await try_session_memory_compaction(
        messages=ordinary_messages(),
        session_memory=revision(),
        context=context(extraction_started_at=NOW, extraction_waiter=waiter),
    )

    assert result is not None
    assert "fresh memory" in str(result.summary_messages[0].content)
    assert calls == ["waited"]


@pytest.mark.asyncio
async def test_extraction_older_than_sixty_seconds_is_ignored() -> None:
    called = False

    async def waiter():
        nonlocal called
        called = True
        return revision(content="must not be used")

    result = await try_session_memory_compaction(
        messages=ordinary_messages(),
        session_memory=revision(content="stable memory"),
        context=context(
            extraction_started_at=NOW - timedelta(seconds=60, microseconds=1),
            extraction_waiter=waiter,
        ),
    )

    assert result is not None
    assert "stable memory" in str(result.summary_messages[0].content)
    assert called is False


@pytest.mark.asyncio
async def test_extraction_timeout_falls_back_to_l3() -> None:
    async def waiter():
        raise TimeoutError

    assert await try_session_memory_compaction(
        messages=ordinary_messages(),
        session_memory=revision(),
        context=context(extraction_started_at=NOW, extraction_waiter=waiter),
    ) is None


def test_coverage_must_match_a_sequence_boundary() -> None:
    messages = ordinary_messages(4)
    assert find_coverage_index(messages, 2) == 1
    assert find_coverage_index(messages, 5) is None


@pytest.mark.asyncio
async def test_recent_tail_expands_backwards_and_result_order_is_replacement_order() -> None:
    result = await try_session_memory_compaction(
        messages=ordinary_messages(),
        session_memory=revision(through=6),
        context=context(history_turns=3),
    )

    assert result is not None
    assert result.messages_to_keep[0].model_extra["stm_sequence_through"] == 3
    post = build_post_compact_messages(result)
    assert post == (
        result.boundary_marker,
        *result.summary_messages,
        *result.messages_to_keep,
        *result.attachments,
        *result.hook_results,
    )


@pytest.mark.asyncio
async def test_l4_keeps_matching_tool_use_when_tail_starts_at_tool_result() -> None:
    messages = (
        message("user", "old", sequence=1),
        message("assistant", "covered", sequence=2),
        message(
            "assistant",
            [{"type": "tool_use", "id": "call-9", "name": "Read", "input": {}}],
            sequence=3,
            tokens=2_000,
        ),
        message(
            "user",
            [{"type": "tool_result", "tool_use_id": "call-9", "content": "ok"}],
            sequence=4,
            tokens=10_000,
        ),
        message("assistant", "done", sequence=5, tokens=2_000),
    )
    result = await try_session_memory_compaction(
        messages=messages,
        session_memory=revision(through=3),
        context=context(history_turns=1),
    )

    assert result is not None
    kept = str(tuple(item.content for item in result.messages_to_keep))
    assert "call-9" in kept and "tool_result" in kept


@pytest.mark.asyncio
async def test_recent_turn_union_still_obeys_forty_thousand_token_cap() -> None:
    messages = ordinary_messages(12, tokens=5_000)
    result = await try_session_memory_compaction(
        messages=messages,
        session_memory=revision(through=10),
        context=context(history_turns=10),
    )

    assert result is not None
    assert FieldEstimator().estimate(
        tuple(item.model_dump(mode="json") for item in result.messages_to_keep)
    ) <= 40_000


@pytest.mark.asyncio
async def test_success_records_true_post_token_count_below_threshold() -> None:
    result = await try_session_memory_compaction(
        messages=ordinary_messages(),
        session_memory=revision(),
        context=context(auto_compact_threshold=100_000),
    )

    assert result is not None
    assert result.true_post_compact_token_count == FieldEstimator().estimate(
        tuple(item.model_dump(mode="json") for item in build_post_compact_messages(result))
    )
    assert result.true_post_compact_token_count < 100_000


@pytest.mark.asyncio
async def test_l4_result_over_threshold_falls_back_to_l3() -> None:
    assert await try_session_memory_compaction(
        messages=ordinary_messages(),
        session_memory=revision(),
        context=context(auto_compact_threshold=1),
    ) is None


@pytest.mark.asyncio
async def test_oversized_memory_section_is_truncated_before_injection() -> None:
    oversized = "# Session Title\n" + ("x" * 8_001)
    result = await try_session_memory_compaction(
        messages=ordinary_messages(),
        session_memory=revision(content=oversized),
        context=context(),
    )

    assert result is not None
    content = str(result.summary_messages[0].content)
    assert "[... section truncated for length ...]" in content
    assert "redis://current-session/session-memory" in content
    assert "x" * 8_001 not in content

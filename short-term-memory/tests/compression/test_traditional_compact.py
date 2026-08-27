from datetime import datetime, timezone

import pytest

from short_term_memory.compression.continuity_model import (
    CompactionModelResponse,
    PromptTooLongError,
)
from short_term_memory.compression.traditional_compact import (
    COMPACT_MAX_OUTPUT_TOKENS,
    MAX_PTL_RETRIES,
    PTL_RETRY_MARKER,
    TraditionalCompactContext,
    build_post_compact_messages,
    compact_conversation,
    partial_compact_conversation,
    truncate_head_for_ptl_retry,
)
from short_term_memory.models import SessionCompressionMessage


NOW = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)


def message(role: str, content: object, *, tokens: int = 1_000, **extra):
    return SessionCompressionMessage(
        role=role, content=content, test_tokens=tokens, **extra
    )


class FieldEstimator:
    def estimate(self, messages):
        return sum(int(item.get("test_tokens", 0)) for item in messages)


class FakeModel:
    def __init__(self, outcomes=None):
        self.outcomes = list(
            outcomes
            or [
                CompactionModelResponse(
                    content="<analysis>draft</analysis><summary>ABCD</summary>",
                    input_tokens=4_000,
                    output_tokens=500,
                )
            ]
        )
        self.calls = []

    async def compact(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def context(model=None, **updates):
    values = {
        "model": model or FakeModel(),
        "model_name": "claude-sonnet",
        "token_estimator": FieldEstimator(),
        "clock": lambda: NOW,
    }
    values.update(updates)
    return TraditionalCompactContext(**values)


def three_rounds():
    return (
        message("user", "u1"),
        message("assistant", "a1"),
        message("user", "u2"),
        message("assistant", "a2"),
        message("user", "u3"),
        message("assistant", "a3"),
    )


@pytest.mark.asyncio
async def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="Not enough messages"):
        await compact_conversation((), context())


@pytest.mark.asyncio
async def test_compact_uses_isolated_one_turn_text_request_and_replacement_order() -> None:
    model = FakeModel()
    attachments = (message("system", "attachment", attachment=True),)
    hooks = (message("system", "hook", hook_result=True),)
    ctx = context(model, attachments=attachments, hook_results=hooks)

    result = await compact_conversation(three_rounds(), ctx, is_auto_compact=True)

    call = model.calls[0]
    assert call["query_source"] == "compact"
    assert call["max_output_tokens"] == COMPACT_MAX_OUTPUT_TOKENS == 20_000
    assert call["model"] == "claude-sonnet"
    assert "Do NOT call any tools" in call["prompt"]
    assert call["messages"] == tuple(
        item.model_dump(mode="json") for item in three_rounds()
    )
    assert build_post_compact_messages(result) == (
        result.boundary_marker,
        *result.summary_messages,
        *result.messages_to_keep,
        *attachments,
        *hooks,
    )
    assert result.post_compact_token_count == 4_500
    assert result.true_post_compact_token_count == FieldEstimator().estimate(
        tuple(item.model_dump(mode="json") for item in build_post_compact_messages(result))
    )


@pytest.mark.asyncio
async def test_second_compact_summarizes_previous_summary_not_full_journal() -> None:
    model = FakeModel()
    active = (
        message("user", "summary AB", is_compact_summary=True),
        message("assistant", "generation CD"),
        message("user", "recent tail"),
    )

    await compact_conversation(active, context(model))

    assert model.calls[0]["messages"][0]["content"] == "summary AB"
    assert all(
        "journal original A" not in str(item)
        for item in model.calls[0]["messages"]
    )


@pytest.mark.asyncio
async def test_boundary_coverage_uses_latest_journal_sequence_in_compacted_input() -> None:
    active = (
        message("user", "summary AB", stm_sequence_through=2),
        message("assistant", "generation CD", stm_sequence_through=4),
        message("user", "recent E", stm_sequence_through=5),
    )

    result = await compact_conversation(active, context())

    boundary = result.boundary_marker.model_extra["compact_boundary"]
    assert boundary["covered_through_sequence"] == 5


@pytest.mark.asyncio
async def test_partial_from_summarizes_tail_and_keeps_prefix() -> None:
    result = await partial_compact_conversation(
        three_rounds(), 4, context(), direction="from"
    )
    assert result.messages_to_keep == three_rounds()[:4]
    assert "RECENT portion" in result.compact_prompt


@pytest.mark.asyncio
async def test_partial_up_to_summarizes_prefix_and_strips_stale_compact_entries() -> None:
    stale_boundary = message(
        "system", "old boundary", compact_boundary={"boundary_id": "old"}
    )
    stale_summary = message("user", "old summary", is_compact_summary=True)
    suffix = (stale_boundary, stale_summary, message("user", "keep me"))
    all_messages = (*three_rounds()[:2], *suffix)

    result = await partial_compact_conversation(
        all_messages, 2, context(), direction="up_to"
    )

    assert result.messages_to_keep == (suffix[-1],)
    assert "Context for Continuing Work" in result.compact_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "pivot", "error"),
    [("from", 2, "after"), ("up_to", 0, "before")],
)
async def test_partial_rejects_empty_summarize_side(direction, pivot, error) -> None:
    with pytest.raises(ValueError, match=error):
        await partial_compact_conversation(
            three_rounds()[:2], pivot, context(), direction=direction
        )


def test_truncate_head_for_ptl_retry_drops_complete_rounds_for_token_gap() -> None:
    truncated = truncate_head_for_ptl_retry(
        three_rounds(), token_gap=1_500, estimator=FieldEstimator()
    )
    assert truncated is not None
    assert tuple(item.content for item in truncated) == (
        PTL_RETRY_MARKER,
        "a2",
        "u3",
        "a3",
    )


def test_unparseable_gap_drops_twenty_percent_but_at_least_one_round() -> None:
    messages = tuple(
        item
        for index in range(10)
        for item in (message("user", f"u{index}"), message("assistant", f"a{index}"))
    )
    truncated = truncate_head_for_ptl_retry(
        messages, token_gap=None, estimator=FieldEstimator()
    )
    assert truncated[0].role == "user"
    assert truncated[0].content == PTL_RETRY_MARKER
    assert truncated[1:] == messages[3:]


def test_assistant_first_remainder_gets_synthetic_user_marker() -> None:
    messages = (
        message("system", "preamble"),
        message("assistant", "a1"),
        message("user", "u2"),
        message("assistant", "a2"),
    )
    truncated = truncate_head_for_ptl_retry(
        messages, token_gap=1, estimator=FieldEstimator()
    )
    assert truncated is not None
    assert truncated[0].role == "user" and truncated[0].content == PTL_RETRY_MARKER


def test_previous_retry_marker_is_removed_and_one_group_cannot_be_truncated() -> None:
    marked = (
        message("user", PTL_RETRY_MARKER, is_meta=True),
        *three_rounds(),
    )
    truncated = truncate_head_for_ptl_retry(
        marked, token_gap=None, estimator=FieldEstimator()
    )
    assert truncated is not None
    assert sum(item.content == PTL_RETRY_MARKER for item in truncated) <= 1
    assert truncate_head_for_ptl_retry(
        (message("user", "only group"),),
        token_gap=1,
        estimator=FieldEstimator(),
    ) is None


@pytest.mark.asyncio
async def test_prompt_too_long_retries_exactly_three_times_then_fails() -> None:
    model = FakeModel([PromptTooLongError() for _ in range(MAX_PTL_RETRIES + 1)])

    with pytest.raises(RuntimeError, match="Conversation too long"):
        await compact_conversation(three_rounds(), context(model))

    assert len(model.calls) == MAX_PTL_RETRIES + 1

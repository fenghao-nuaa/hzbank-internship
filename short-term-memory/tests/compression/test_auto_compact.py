import pytest

from short_term_memory.compression.auto_compact import (
    AUTOCOMPACT_BUFFER_TOKENS,
    MANUAL_COMPACT_BUFFER_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    AutoCompactContext,
    ModelProfile,
    auto_compact_if_needed,
    auto_compact_threshold,
    effective_context_window,
    manual_compact_threshold,
)
from short_term_memory.compression.session_memory_compact import CompactionResult
from short_term_memory.models import (
    AutoCompactTrackingState,
    SessionCompressionMessage,
)


class ConstantEstimator:
    def __init__(self, tokens):
        self.tokens = tokens

    def estimate(self, messages):
        del messages
        return self.tokens


def compact_result(tokens=1_000):
    marker = SessionCompressionMessage(role="system", content="boundary")
    return CompactionResult(
        boundary_marker=marker,
        summary_messages=(SessionCompressionMessage(role="user", content="summary"),),
        messages_to_keep=(),
        true_post_compact_token_count=tokens,
    )


def context(calls, *, tokens=200_000, l4=None, l3=None, query_source="main"):
    async def default_l4(messages, threshold):
        del messages, threshold
        calls.append("l4")
        return l4

    async def default_l3(messages, tracking):
        del messages, tracking
        calls.append("l3")
        if isinstance(l3, Exception):
            raise l3
        return l3 or compact_result()

    return AutoCompactContext(
        model_profile=ModelProfile(
            context_window_tokens=200_000, max_output_tokens=32_000
        ),
        token_estimator=ConstantEstimator(tokens),
        query_source=query_source,
        try_session_memory=default_l4,
        compact_conversation=default_l3,
    )


def test_auto_threshold_matches_claude_constants() -> None:
    profile = ModelProfile(context_window_tokens=200_000, max_output_tokens=32_000)
    assert effective_context_window(profile) == 180_000
    assert auto_compact_threshold(profile) == 167_000
    assert manual_compact_threshold(profile) == 177_000
    assert AUTOCOMPACT_BUFFER_TOKENS == 13_000
    assert MANUAL_COMPACT_BUFFER_TOKENS == 3_000


def test_output_reserve_is_capped_at_twenty_thousand() -> None:
    small = ModelProfile(context_window_tokens=100_000, max_output_tokens=8_000)
    large = ModelProfile(context_window_tokens=100_000, max_output_tokens=50_000)
    assert effective_context_window(small) == 92_000
    assert effective_context_window(large) == 80_000


@pytest.mark.asyncio
async def test_below_threshold_makes_no_compact_calls() -> None:
    calls = []
    result = await auto_compact_if_needed(
        messages=(SessionCompressionMessage(role="user", content="x"),),
        context=context(calls, tokens=166_999),
        tracking=AutoCompactTrackingState(),
    )
    assert not result.was_compacted and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["compact", "session_memory"])
async def test_forked_query_sources_never_recurse(source) -> None:
    calls = []
    result = await auto_compact_if_needed(
        messages=(SessionCompressionMessage(role="user", content="x"),),
        context=context(calls, query_source=source),
        tracking=AutoCompactTrackingState(),
    )
    assert not result.was_compacted and calls == []


@pytest.mark.asyncio
async def test_three_failures_trip_automatic_circuit_breaker() -> None:
    calls = []
    tracking = AutoCompactTrackingState(consecutive_failures=3)
    result = await auto_compact_if_needed(
        messages=(SessionCompressionMessage(role="user", content="x"),),
        context=context(calls),
        tracking=tracking,
    )
    assert MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES == 3
    assert result.tracking == tracking and calls == []


@pytest.mark.asyncio
async def test_l4_success_skips_l3_and_resets_tracking() -> None:
    calls = []
    result = await auto_compact_if_needed(
        messages=(SessionCompressionMessage(role="user", content="x"),),
        context=context(calls, l4=compact_result()),
        tracking=AutoCompactTrackingState(consecutive_failures=2),
    )
    assert result.was_compacted and calls == ["l4"]
    assert result.tracking.consecutive_failures == 0
    assert result.tracking.compacted is True and result.tracking.turn_id


@pytest.mark.asyncio
async def test_l4_none_or_over_threshold_invokes_l3() -> None:
    for l4 in (None, compact_result(tokens=167_000)):
        calls = []
        result = await auto_compact_if_needed(
            messages=(SessionCompressionMessage(role="user", content="x"),),
            context=context(calls, l4=l4),
            tracking=AutoCompactTrackingState(),
        )
        assert result.was_compacted and calls == ["l4", "l3"]


@pytest.mark.asyncio
async def test_l3_error_increments_failure_exactly_once() -> None:
    calls = []
    result = await auto_compact_if_needed(
        messages=(SessionCompressionMessage(role="user", content="x"),),
        context=context(calls, l3=RuntimeError("failed")),
        tracking=AutoCompactTrackingState(consecutive_failures=1),
    )
    assert not result.was_compacted
    assert result.tracking.consecutive_failures == 2
    assert calls == ["l4", "l3"]


@pytest.mark.asyncio
async def test_manual_path_bypasses_only_automatic_failure_breaker() -> None:
    calls = []
    result = await auto_compact_if_needed(
        messages=(SessionCompressionMessage(role="user", content="x"),),
        context=context(calls, tokens=176_999),
        tracking=AutoCompactTrackingState(consecutive_failures=3),
        manual=True,
    )
    assert not result.was_compacted and calls == []

    result = await auto_compact_if_needed(
        messages=(SessionCompressionMessage(role="user", content="x"),),
        context=context(calls, tokens=177_000),
        tracking=AutoCompactTrackingState(consecutive_failures=3),
        manual=True,
    )
    assert result.was_compacted and calls == ["l4", "l3"]

from datetime import datetime, timedelta, timezone

import pytest

from short_term_memory.compression.session_memory_state import (
    SessionMemoryConfig,
    is_extraction_stale,
    should_extract_memory,
    should_extract_memory_from_counts,
)


def test_session_memory_defaults_match_claude_source() -> None:
    config = SessionMemoryConfig()

    assert config.minimum_message_tokens_to_init == 10_000
    assert config.minimum_tokens_between_update == 5_000
    assert config.tool_calls_between_updates == 3
    assert config.wait_for_extraction_seconds == 15.0
    assert config.stale_extraction_seconds == 60.0
    assert config.max_section_tokens == 2_000
    assert config.max_total_tokens == 12_000


@pytest.mark.parametrize(
    ("growth", "tool_calls", "last_has_tools", "expected"),
    [
        (False, 3, True, False),
        (True, 3, True, True),
        (True, 0, False, True),
        (True, 0, True, False),
    ],
)
def test_should_extract_memory_matches_claude_truth_table(
    growth: bool, tool_calls: int, last_has_tools: bool, expected: bool
) -> None:
    assert (
        should_extract_memory_from_counts(
            token_growth_reached=growth,
            tool_calls_since_update=tool_calls,
            last_assistant_turn_has_tool_calls=last_has_tools,
        )
        is expected
    )


def test_should_extract_memory_requires_initialization_threshold() -> None:
    assert (
        should_extract_memory(
            current_token_count=9_999,
            tokens_at_last_extraction=0,
            tool_calls_since_update=10,
            last_assistant_turn_has_tool_calls=False,
            initialized=False,
        )
        is False
    )
    assert (
        should_extract_memory(
            current_token_count=10_000,
            tokens_at_last_extraction=0,
            tool_calls_since_update=0,
            last_assistant_turn_has_tool_calls=False,
            initialized=False,
        )
        is True
    )


def test_should_extract_memory_uses_context_growth_not_cumulative_usage() -> None:
    assert (
        should_extract_memory(
            current_token_count=14_999,
            tokens_at_last_extraction=10_000,
            tool_calls_since_update=3,
            last_assistant_turn_has_tool_calls=True,
            initialized=True,
        )
        is False
    )
    assert (
        should_extract_memory(
            current_token_count=15_000,
            tokens_at_last_extraction=10_000,
            tool_calls_since_update=3,
            last_assistant_turn_has_tool_calls=True,
            initialized=True,
        )
        is True
    )


def test_extraction_is_stale_only_after_sixty_seconds() -> None:
    now = datetime(2026, 8, 14, tzinfo=timezone.utc)

    assert is_extraction_stale(None, now=now) is False
    assert is_extraction_stale(now - timedelta(seconds=60), now=now) is False
    assert is_extraction_stale(now - timedelta(seconds=60, microseconds=1), now=now)

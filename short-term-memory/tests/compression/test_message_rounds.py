from short_term_memory.compression.context_messages import annotate_active_message
from short_term_memory.compression.message_rounds import (
    adjust_index_to_preserve_api_invariants,
    calculate_messages_to_keep_index,
    get_messages_after_compact_boundary,
    group_messages_by_api_round,
)
from short_term_memory.models import SessionCompressionMessage


def message(role: str, content="", **extra) -> SessionCompressionMessage:
    return SessionCompressionMessage(role=role, content=content, **extra)


class FixedEstimator:
    def estimate(self, messages):
        return sum(int(item.get("test_tokens", 0)) for item in messages)


def test_get_messages_after_last_compact_boundary_includes_boundary() -> None:
    messages = (
        message("user", "old"),
        message("system", "b1", compact_boundary={"id": "one"}),
        message("user", "middle"),
        message("system", "b2", compact_boundary={"id": "two"}),
        message("user", "tail"),
    )

    assert get_messages_after_compact_boundary(messages) == messages[3:]


def test_adjust_index_pulls_matching_tool_use_and_same_message_id() -> None:
    messages = (
        message("assistant", [{"type": "thinking", "thinking": "x"}], message_id="a1"),
        message(
            "assistant",
            [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}],
            message_id="a1",
        ),
        message("user", [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]),
        message("assistant", "done"),
    )

    assert adjust_index_to_preserve_api_invariants(messages, 2) == 0


def test_adjust_index_never_splits_internal_generation_group() -> None:
    messages = tuple(
        annotate_active_message(
            message("assistant", str(index)),
            from_sequence=1,
            through_sequence=2,
            group_id="generation:1",
        )
        for index in range(3)
    ) + (message("user", "tail"),)

    assert adjust_index_to_preserve_api_invariants(messages, 2) == 0


def test_group_messages_by_api_round_keeps_each_user_turn_together() -> None:
    messages = (
        message("system", "preamble"),
        message("user", "u1"),
        message("assistant", "a1"),
        message("tool", "t1"),
        message("user", "u2"),
        message("assistant", "a2"),
    )

    assert group_messages_by_api_round(messages) == (
        messages[:1], messages[1:4], messages[4:]
    )


def test_tail_selection_expands_to_minimums_without_crossing_boundary() -> None:
    boundary = message("system", "boundary", compact_boundary={"id": "b"})
    messages = (message("user", "before", test_tokens=50_000), boundary) + tuple(
        message("user" if index % 2 == 0 else "assistant", str(index), test_tokens=2_000)
        for index in range(6)
    )

    start = calculate_messages_to_keep_index(
        messages,
        last_summarized_index=6,
        estimator=FixedEstimator(),
        recent_user_turns=1,
    )

    assert start == 3

from datetime import datetime, timedelta, timezone

from short_term_memory.compression.micro_compact import (
    IMAGE_MAX_TOKEN_SIZE,
    TIME_BASED_MC_CLEARED_MESSAGE,
    TimeBasedMicroCompactConfig,
    calculate_tool_result_tokens,
    estimate_message_tokens,
    evaluate_time_based_trigger,
    microcompact_messages,
)
from short_term_memory.models import SessionCompressionMessage


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(minutes=60)


def assistant(tool_id: str, name: str = "Read", *, timestamp=OLD):
    values = {
        "role": "assistant",
        "content": ({"type": "tool_use", "id": tool_id, "name": name, "input": {}},),
    }
    if timestamp is not None:
        values["stm_timestamp"] = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    return SessionCompressionMessage.model_validate(values)


def result(tool_id: str, content: object):
    return SessionCompressionMessage(
        role="user",
        content=({"type": "tool_result", "tool_use_id": tool_id, "content": content},),
    )


def conversation(*, names=("Read", "Grep", "Bash"), contents=None):
    contents = contents or ("first full result", "second full result", "latest full result")
    messages = []
    for index, (name, content) in enumerate(zip(names, contents, strict=True), start=1):
        messages.extend((assistant(str(index), name), result(str(index), content)))
    return tuple(messages)


def tool_result_content(messages, tool_id: str):
    for message in messages:
        for block in message.content if isinstance(message.content, tuple) else ():
            if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_id:
                return block.get("content")
    raise AssertionError(f"missing tool result {tool_id}")


def enabled(**updates) -> TimeBasedMicroCompactConfig:
    return TimeBasedMicroCompactConfig(
        enabled=True,
        gap_threshold_minutes=updates.get("gap_threshold_minutes", 30),
        keep_recent=updates.get("keep_recent", 1),
    )


def test_config_defaults_match_claude_remote_config_defaults() -> None:
    assert TimeBasedMicroCompactConfig() == TimeBasedMicroCompactConfig(
        enabled=False, gap_threshold_minutes=60, keep_recent=5
    )


def test_trigger_requires_explicit_main_source_and_valid_assistant_timestamp() -> None:
    messages = (assistant("1"), result("1", "full"))
    assert evaluate_time_based_trigger(messages, None, now=NOW, config=enabled()) is None
    assert evaluate_time_based_trigger(messages, "session_memory", now=NOW, config=enabled()) is None
    assert evaluate_time_based_trigger((result("1", "full"),), "main", now=NOW, config=enabled()) is None
    invalid = (assistant("1", timestamp="not-a-date"), result("1", "full"))
    assert evaluate_time_based_trigger(invalid, "main", now=NOW, config=enabled()) is None


def test_gap_equal_or_above_threshold_fires_but_below_does_not() -> None:
    messages = (assistant("1"), result("1", "full"))
    assert evaluate_time_based_trigger(
        messages, "main", now=OLD + timedelta(minutes=29, seconds=59), config=enabled()
    ) is None
    trigger = evaluate_time_based_trigger(
        messages, "main:output-style", now=OLD + timedelta(minutes=30), config=enabled()
    )
    assert trigger is not None and trigger.gap_minutes == 30


def test_time_based_microcompact_clears_old_results_and_keeps_latest() -> None:
    messages = conversation()
    compacted = microcompact_messages(messages, "main", now=NOW, config=enabled())
    assert tool_result_content(compacted.messages, "1") == TIME_BASED_MC_CLEARED_MESSAGE
    assert tool_result_content(compacted.messages, "2") == TIME_BASED_MC_CLEARED_MESSAGE
    assert tool_result_content(compacted.messages, "3") == "latest full result"
    assert compacted.tokens_saved > 0
    assert tool_result_content(messages, "1") == "first full result"


def test_only_known_tools_compact_and_keep_recent_has_floor_of_one() -> None:
    messages = conversation(names=("Read", "CustomTool", "Grep"))
    compacted = microcompact_messages(
        messages, "main", now=NOW, config=enabled(keep_recent=0)
    )
    assert tool_result_content(compacted.messages, "1") == TIME_BASED_MC_CLEARED_MESSAGE
    assert tool_result_content(compacted.messages, "2") == "second full result"
    assert tool_result_content(compacted.messages, "3") == "latest full result"


def test_already_cleared_zero_savings_returns_original_tuple() -> None:
    messages = conversation(
        names=("Read", "Grep"),
        contents=(TIME_BASED_MC_CLEARED_MESSAGE, "latest full result"),
    )
    compacted = microcompact_messages(messages, "main", now=NOW, config=enabled())
    assert compacted.messages is messages
    assert compacted.tokens_saved == 0


def test_tool_result_image_and_document_each_count_as_2000_tokens() -> None:
    block = {
        "type": "tool_result",
        "tool_use_id": "1",
        "content": (
            {"type": "image", "source": {}},
            {"type": "document", "source": {}},
            {"type": "text", "text": "12345678"},
        ),
    }
    assert calculate_tool_result_tokens(block) == IMAGE_MAX_TOKEN_SIZE * 2 + 2


def test_message_estimate_covers_text_thinking_tool_use_and_four_thirds_padding() -> None:
    messages = (
        SessionCompressionMessage(
            role="assistant",
            content=(
                {"type": "text", "text": "12345678"},
                {"type": "thinking", "thinking": "12345678", "signature": "ignored"},
                {"type": "redacted_thinking", "data": "1234"},
                {"type": "tool_use", "id": "ignored", "name": "Read", "input": {}},
                {"type": "image", "source": {}},
            ),
        ),
    )
    # rough: 2 + 2 + 1 + round(len('Read{}') / 4)=2 + 2000; then ceil(4/3).
    assert estimate_message_tokens(messages) == 2676

"""Claude compact boundary and API-round preservation algorithms.

Claude source: utils/messages.ts:getMessagesAfterCompactBoundary and
services/compact/sessionMemoryCompact.ts:
adjustIndexToPreserveAPIInvariants/calculateMessagesToKeepIndex.
"""

from typing import Any, Protocol

from short_term_memory.compression.context_messages import to_provider_messages
from short_term_memory.models import SessionCompressionMessage

DEFAULT_MIN_TOKENS = 10_000
DEFAULT_MIN_TEXT_BLOCK_MESSAGES = 5
DEFAULT_MAX_TOKENS = 40_000


class TokenEstimator(Protocol):
    def estimate(self, messages: tuple[dict[str, Any], ...]) -> int: ...


def _extra(message: SessionCompressionMessage, key: str) -> Any:
    return (message.model_extra or {}).get(key)


def _blocks(message: SessionCompressionMessage) -> tuple[dict[str, Any], ...]:
    content = message.content
    if not isinstance(content, (list, tuple)):
        return ()
    return tuple(block for block in content if isinstance(block, dict))


def _has_text_blocks(message: SessionCompressionMessage) -> bool:
    if isinstance(message.content, str):
        return bool(message.content)
    return any(block.get("type") == "text" for block in _blocks(message))


def _message_tokens(message: SessionCompressionMessage, estimator: TokenEstimator) -> int:
    return estimator.estimate(to_provider_messages((message,)))


def _is_boundary(message: SessionCompressionMessage) -> bool:
    return bool(_extra(message, "compact_boundary"))


def get_messages_after_compact_boundary(
    messages: tuple[SessionCompressionMessage, ...],
) -> tuple[SessionCompressionMessage, ...]:
    boundary = next(
        (index for index in range(len(messages) - 1, -1, -1) if _is_boundary(messages[index])),
        -1,
    )
    return messages if boundary == -1 else messages[boundary:]


def _tool_result_ids(message: SessionCompressionMessage) -> tuple[str, ...]:
    if message.role != "user":
        return ()
    return tuple(
        str(block["tool_use_id"])
        for block in _blocks(message)
        if block.get("type") == "tool_result" and block.get("tool_use_id")
    )


def _tool_use_ids(message: SessionCompressionMessage) -> tuple[str, ...]:
    if message.role != "assistant":
        return ()
    return tuple(
        str(block["id"])
        for block in _blocks(message)
        if block.get("type") == "tool_use" and block.get("id")
    )


def _expand_group(
    messages: tuple[SessionCompressionMessage, ...], start: int
) -> int:
    if start <= 0 or start >= len(messages):
        return start
    group_id = _extra(messages[start], "stm_group_id")
    while start > 0 and group_id and _extra(messages[start - 1], "stm_group_id") == group_id:
        start -= 1
    return start


def adjust_index_to_preserve_api_invariants(
    messages: tuple[SessionCompressionMessage, ...], start_index: int
) -> int:
    if start_index <= 0 or start_index >= len(messages):
        return start_index
    adjusted = _expand_group(messages, start_index)
    results = {
        result_id
        for message in messages[start_index:]
        for result_id in _tool_result_ids(message)
    }
    uses_in_tail = {
        use_id for message in messages[adjusted:] for use_id in _tool_use_ids(message)
    }
    needed = results - uses_in_tail
    for index in range(adjusted - 1, -1, -1):
        found = needed.intersection(_tool_use_ids(messages[index]))
        if found:
            adjusted = index
            needed -= found
    message_ids = {
        str(_extra(message, "message_id"))
        for message in messages[adjusted:]
        if message.role == "assistant" and _extra(message, "message_id")
    }
    for index in range(adjusted - 1, -1, -1):
        if messages[index].role == "assistant" and _extra(messages[index], "message_id") in message_ids:
            adjusted = index
    return _expand_group(messages, adjusted)


def group_messages_by_api_round(
    messages: tuple[SessionCompressionMessage, ...],
) -> tuple[tuple[SessionCompressionMessage, ...], ...]:
    groups: list[list[SessionCompressionMessage]] = []
    current: list[SessionCompressionMessage] = []
    for message in messages:
        if message.role == "user" and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return tuple(tuple(group) for group in groups)


def _recent_turn_start(
    messages: tuple[SessionCompressionMessage, ...], turns: int, floor: int
) -> int:
    seen = 0
    for index in range(len(messages) - 1, floor - 1, -1):
        if messages[index].role == "user":
            seen += 1
            if seen == turns:
                return index
    return floor


def calculate_messages_to_keep_index(
    messages: tuple[SessionCompressionMessage, ...],
    last_summarized_index: int,
    estimator: TokenEstimator,
    *,
    recent_user_turns: int,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    min_text_messages: int = DEFAULT_MIN_TEXT_BLOCK_MESSAGES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> int:
    if not messages:
        return 0
    start = last_summarized_index + 1 if last_summarized_index >= 0 else len(messages)
    total = sum(_message_tokens(message, estimator) for message in messages[start:])
    text_count = sum(int(_has_text_blocks(message)) for message in messages[start:])
    last_boundary = next(
        (index for index in range(len(messages) - 1, -1, -1) if _is_boundary(messages[index])),
        -1,
    )
    floor = last_boundary + 1
    while start > floor and total < max_tokens:
        if total >= min_tokens and text_count >= min_text_messages:
            break
        start -= 1
        total += _message_tokens(messages[start], estimator)
        text_count += int(_has_text_blocks(messages[start]))
    start = min(start, _recent_turn_start(messages, recent_user_turns, floor))
    while start < len(messages) and estimator.estimate(to_provider_messages(messages[start:])) > max_tokens:
        groups = group_messages_by_api_round(messages[start:])
        if len(groups) <= 1:
            break
        start += len(groups[0])
    return adjust_index_to_preserve_api_invariants(messages, start)

"""Portable Claude time-based microcompact for the request projection only.

Source parity: ``services/compact/microCompact.ts``. The Anthropic-only cached
microcompact branch is intentionally excluded because it edits provider cache
blocks rather than local messages.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import math
from typing import Any

from short_term_memory.models import SessionCompressionMessage


TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"
IMAGE_MAX_TOKEN_SIZE = 2_000
COMPACTABLE_TOOLS = frozenset(
    {
        "Read",
        "Bash",
        "PowerShell",
        "Grep",
        "Glob",
        "WebSearch",
        "WebFetch",
        "Edit",
        "Write",
    }
)


@dataclass(frozen=True)
class TimeBasedMicroCompactConfig:
    enabled: bool = False
    gap_threshold_minutes: float = 60.0
    keep_recent: int = 5

    def __post_init__(self) -> None:
        if self.gap_threshold_minutes <= 0:
            raise ValueError("gap_threshold_minutes must be positive")
        if self.keep_recent < 0:
            raise ValueError("keep_recent must not be negative")


@dataclass(frozen=True)
class TimeBasedTrigger:
    gap_minutes: float
    config: TimeBasedMicroCompactConfig


@dataclass(frozen=True)
class MicroCompactResult:
    messages: tuple[SessionCompressionMessage, ...]
    tokens_saved: int = 0
    tools_cleared: int = 0
    tools_kept: int = 0


def _rough_token_count(content: str) -> int:
    # TypeScript Math.round() for a non-negative string length, not bankers' round.
    return math.floor(len(content) / 4 + 0.5)


def _json_stringify(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _blocks(content: Any) -> Sequence[Any] | None:
    if isinstance(content, Sequence) and not isinstance(
        content, (str, bytes, bytearray)
    ):
        return content
    return None


def calculate_tool_result_tokens(block: Mapping[str, Any]) -> int:
    content = block.get("content")
    if content is None or content == "":
        return 0
    if isinstance(content, str):
        return _rough_token_count(content)
    blocks = _blocks(content)
    if blocks is None:
        return 0
    total = 0
    for item in blocks:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "text":
            total += _rough_token_count(str(item.get("text", "")))
        elif kind in {"image", "document"}:
            total += IMAGE_MAX_TOKEN_SIZE
    return total


def estimate_message_tokens(
    messages: tuple[SessionCompressionMessage, ...],
) -> int:
    """Translate Claude's conservative 4/3 padded message estimate."""

    total = 0
    for message in messages:
        if message.role not in {"user", "assistant"}:
            continue
        blocks = _blocks(message.content)
        if blocks is None:
            continue
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "text":
                total += _rough_token_count(str(block.get("text", "")))
            elif kind == "tool_result":
                total += calculate_tool_result_tokens(block)
            elif kind in {"image", "document"}:
                total += IMAGE_MAX_TOKEN_SIZE
            elif kind == "thinking":
                total += _rough_token_count(str(block.get("thinking", "")))
            elif kind == "redacted_thinking":
                total += _rough_token_count(str(block.get("data", "")))
            elif kind == "tool_use":
                total += _rough_token_count(
                    str(block.get("name", ""))
                    + _json_stringify(block.get("input") or {})
                )
            else:
                total += _rough_token_count(_json_stringify(block))
    return math.ceil(total * (4 / 3))


def _timestamp(message: SessionCompressionMessage) -> datetime | None:
    extra = message.model_extra or {}
    raw = extra.get("stm_timestamp", extra.get("timestamp"))
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def evaluate_time_based_trigger(
    messages: tuple[SessionCompressionMessage, ...],
    query_source: str | None,
    *,
    now: datetime,
    config: TimeBasedMicroCompactConfig,
) -> TimeBasedTrigger | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not config.enabled or not query_source or not query_source.startswith("main"):
        return None
    last_assistant = next(
        (message for message in reversed(messages) if message.role == "assistant"),
        None,
    )
    if last_assistant is None:
        return None
    timestamp = _timestamp(last_assistant)
    if timestamp is None:
        return None
    gap_minutes = (now - timestamp).total_seconds() / 60
    if not math.isfinite(gap_minutes) or gap_minutes < config.gap_threshold_minutes:
        return None
    return TimeBasedTrigger(gap_minutes=gap_minutes, config=config)


def _compactable_tool_ids(
    messages: tuple[SessionCompressionMessage, ...],
) -> tuple[str, ...]:
    identifiers: list[str] = []
    for message in messages:
        if message.role != "assistant":
            continue
        for block in _blocks(message.content) or ():
            if (
                isinstance(block, Mapping)
                and block.get("type") == "tool_use"
                and block.get("name") in COMPACTABLE_TOOLS
                and isinstance(block.get("id"), str)
            ):
                identifiers.append(block["id"])
    return tuple(identifiers)


def microcompact_messages(
    messages: tuple[SessionCompressionMessage, ...],
    query_source: str | None,
    *,
    now: datetime,
    config: TimeBasedMicroCompactConfig,
) -> MicroCompactResult:
    trigger = evaluate_time_based_trigger(
        messages, query_source, now=now, config=config
    )
    if trigger is None:
        return MicroCompactResult(messages=messages)

    compactable_ids = _compactable_tool_ids(messages)
    keep_recent = max(1, config.keep_recent)
    keep_set = frozenset(compactable_ids[-keep_recent:])
    clear_set = frozenset(
        identifier for identifier in compactable_ids if identifier not in keep_set
    )
    if not clear_set:
        return MicroCompactResult(messages=messages)

    tokens_saved = 0
    result: list[SessionCompressionMessage] = []
    for message in messages:
        blocks = _blocks(message.content)
        if message.role != "user" or blocks is None:
            result.append(message)
            continue
        touched = False
        new_content: list[Any] = []
        for block in blocks:
            if (
                isinstance(block, Mapping)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") in clear_set
                and block.get("content") != TIME_BASED_MC_CLEARED_MESSAGE
            ):
                tokens_saved += calculate_tool_result_tokens(block)
                new_content.append(
                    {**dict(block), "content": TIME_BASED_MC_CLEARED_MESSAGE}
                )
                touched = True
            else:
                new_content.append(block)
        if not touched:
            result.append(message)
            continue
        raw = message.model_dump(mode="json")
        raw["content"] = new_content
        result.append(SessionCompressionMessage.model_validate(raw))

    if tokens_saved == 0:
        return MicroCompactResult(messages=messages)
    return MicroCompactResult(
        messages=tuple(result),
        tokens_saved=tokens_saved,
        tools_cleared=len(clear_set),
        tools_kept=len(keep_set),
    )

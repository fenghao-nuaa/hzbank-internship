"""Claude L3 traditional and partial compact orchestration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import uuid

from short_term_memory.compression.compact_prompt import (
    get_compact_prompt,
    get_compact_user_summary_message,
    get_partial_compact_prompt,
)
from short_term_memory.compression.context_messages import to_provider_messages
from short_term_memory.compression.continuity_model import (
    ContinuityCompactionModel,
    PromptTooLongError,
)
from short_term_memory.compression.message_rounds import TokenEstimator
from short_term_memory.compression.session_memory_compact import (
    CompactionResult,
    build_post_compact_messages,
)
from short_term_memory.models import CompactBoundary, SessionCompressionMessage

ERROR_MESSAGE_NOT_ENOUGH_MESSAGES = "Not enough messages to compact."
ERROR_MESSAGE_PROMPT_TOO_LONG = (
    "Conversation too long. Press esc twice to go up a few messages and try again."
)
MAX_PTL_RETRIES = 3
PTL_RETRY_MARKER = "[earlier conversation truncated for compaction retry]"
COMPACT_MAX_OUTPUT_TOKENS = 20_000


@dataclass(frozen=True)
class TraditionalCompactContext:
    model: ContinuityCompactionModel
    model_name: str
    token_estimator: TokenEstimator
    attachments: tuple[SessionCompressionMessage, ...] = ()
    hook_results: tuple[SessionCompressionMessage, ...] = ()
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    max_output_tokens: int = COMPACT_MAX_OUTPUT_TOKENS

    def __post_init__(self) -> None:
        if not self.model_name:
            raise ValueError("model_name must not be blank")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")


def _extra(message: SessionCompressionMessage, key: str) -> object:
    return (message.model_extra or {}).get(key)


def _group_messages_by_api_round(
    messages: tuple[SessionCompressionMessage, ...],
) -> tuple[tuple[SessionCompressionMessage, ...], ...]:
    """Port current Claude ``grouping.ts`` assistant-response boundaries."""

    groups: list[list[SessionCompressionMessage]] = []
    current: list[SessionCompressionMessage] = []
    last_assistant_id: object = object()
    for index, message in enumerate(messages):
        message_id = _extra(message, "message_id")
        assistant_identity = message_id if message_id is not None else ("index", index)
        if (
            message.role == "assistant"
            and assistant_identity != last_assistant_id
            and current
        ):
            groups.append(current)
            current = [message]
        else:
            current.append(message)
        if message.role == "assistant":
            last_assistant_id = assistant_identity
    if current:
        groups.append(current)
    return tuple(tuple(group) for group in groups)


def _remove_orphan_tool_results(
    messages: tuple[SessionCompressionMessage, ...],
) -> tuple[SessionCompressionMessage, ...]:
    tool_use_ids = {
        str(block["id"])
        for message in messages
        if message.role == "assistant" and isinstance(message.content, (list, tuple))
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
    }
    repaired: list[SessionCompressionMessage] = []
    for message in messages:
        if message.role != "user" or not isinstance(message.content, (list, tuple)):
            repaired.append(message)
            continue
        blocks = tuple(
            block
            for block in message.content
            if not (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and str(block.get("tool_use_id")) not in tool_use_ids
            )
        )
        if blocks:
            repaired.append(message.model_copy(update={"content": blocks}))
    return tuple(repaired)


def truncate_head_for_ptl_retry(
    messages: tuple[SessionCompressionMessage, ...],
    *,
    token_gap: int | None,
    estimator: TokenEstimator,
) -> tuple[SessionCompressionMessage, ...] | None:
    """Drop oldest complete API rounds using Claude's gap/20% rule."""

    if token_gap is not None and token_gap < 1:
        raise ValueError("token_gap must be positive")
    source = messages
    if (
        source
        and source[0].role == "user"
        and _extra(source[0], "is_meta")
        and source[0].content == PTL_RETRY_MARKER
    ):
        source = source[1:]
    groups = _group_messages_by_api_round(source)
    if len(groups) < 2:
        return None
    if token_gap is None:
        drop_count = max(1, math.floor(len(groups) * 0.2))
    else:
        accumulated = 0
        drop_count = 0
        for group in groups:
            accumulated += estimator.estimate(to_provider_messages(group))
            drop_count += 1
            if accumulated >= token_gap:
                break
    drop_count = min(drop_count, len(groups) - 1)
    if drop_count < 1:
        return None
    sliced = _remove_orphan_tool_results(
        tuple(message for group in groups[drop_count:] for message in group)
    )
    if not sliced:
        return None
    if sliced[0].role == "assistant":
        return (
            SessionCompressionMessage(
                role="user", content=PTL_RETRY_MARKER, is_meta=True
            ),
            *sliced,
        )
    return sliced


async def _compact_summary(
    messages: tuple[SessionCompressionMessage, ...],
    *,
    prompt: str,
    context: TraditionalCompactContext,
) -> tuple[str, int, int]:
    current = messages
    ptl_attempts = 0
    while True:
        try:
            response = await context.model.compact(
                messages=to_provider_messages(current),
                prompt=prompt,
                model=context.model_name,
                max_output_tokens=min(
                    COMPACT_MAX_OUTPUT_TOKENS, context.max_output_tokens
                ),
                query_source="compact",
            )
            return (
                response.content,
                response.input_tokens + response.output_tokens,
                ptl_attempts,
            )
        except PromptTooLongError as error:
            ptl_attempts += 1
            truncated = (
                truncate_head_for_ptl_retry(
                    current,
                    token_gap=error.token_gap,
                    estimator=context.token_estimator,
                )
                if ptl_attempts <= MAX_PTL_RETRIES
                else None
            )
            if truncated is None:
                raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG) from error
            current = truncated


def _boundary(
    *,
    trigger: str,
    covered_through_sequence: int,
    pre_tokens: int,
    post_tokens: int,
    context: TraditionalCompactContext,
) -> SessionCompressionMessage:
    boundary = CompactBoundary(
        boundary_id=uuid.uuid4().hex,
        trigger=trigger,
        strategy="traditional",
        covered_through_sequence=covered_through_sequence,
        pre_compact_tokens=pre_tokens,
        true_post_compact_tokens=post_tokens,
        created_at=context.clock().isoformat(),
    )
    return SessionCompressionMessage(
        role="system",
        content="[compact boundary]",
        compact_boundary=boundary.model_dump(mode="json"),
    )


def _result(
    *,
    all_messages: tuple[SessionCompressionMessage, ...],
    covered_messages: tuple[SessionCompressionMessage, ...],
    raw_summary: str,
    compact_call_tokens: int,
    messages_to_keep: tuple[SessionCompressionMessage, ...],
    prompt: str,
    trigger: str,
    context: TraditionalCompactContext,
) -> CompactionResult:
    pre = context.token_estimator.estimate(to_provider_messages(all_messages))
    covered_through_sequence = max(
        (
            int(_extra(message, "stm_sequence_through") or 0)
            for message in covered_messages
        ),
        default=0,
    )
    summary = get_compact_user_summary_message(raw_summary)
    initial = CompactionResult(
        boundary_marker=_boundary(
            trigger=trigger,
            covered_through_sequence=covered_through_sequence,
            pre_tokens=pre,
            post_tokens=0,
            context=context,
        ),
        summary_messages=(summary,),
        messages_to_keep=messages_to_keep,
        attachments=context.attachments,
        hook_results=context.hook_results,
        pre_compact_token_count=pre,
        post_compact_token_count=compact_call_tokens,
        compact_prompt=prompt,
    )
    true_post = context.token_estimator.estimate(
        to_provider_messages(build_post_compact_messages(initial))
    )
    return CompactionResult(
        **{
            **initial.__dict__,
            "boundary_marker": _boundary(
                trigger=trigger,
                covered_through_sequence=covered_through_sequence,
                pre_tokens=pre,
                post_tokens=true_post,
                context=context,
            ),
            "true_post_compact_token_count": true_post,
        }
    )


async def compact_conversation(
    messages: tuple[SessionCompressionMessage, ...],
    context: TraditionalCompactContext,
    *,
    custom_instructions: str | None = None,
    is_auto_compact: bool = False,
) -> CompactionResult:
    if not messages:
        raise ValueError(ERROR_MESSAGE_NOT_ENOUGH_MESSAGES)
    prompt = get_compact_prompt(custom_instructions)
    summary, call_tokens, _ = await _compact_summary(
        messages, prompt=prompt, context=context
    )
    return _result(
        all_messages=messages,
        covered_messages=messages,
        raw_summary=summary,
        compact_call_tokens=call_tokens,
        messages_to_keep=(),
        prompt=prompt,
        trigger="auto" if is_auto_compact else "manual",
        context=context,
    )


async def partial_compact_conversation(
    all_messages: tuple[SessionCompressionMessage, ...],
    pivot_index: int,
    context: TraditionalCompactContext,
    *,
    user_feedback: str | None = None,
    direction: str = "from",
) -> CompactionResult:
    if direction not in {"from", "up_to"}:
        raise ValueError("direction must be 'from' or 'up_to'")
    if pivot_index < 0 or pivot_index > len(all_messages):
        raise ValueError("pivot_index is out of range")
    to_summarize = (
        all_messages[:pivot_index]
        if direction == "up_to"
        else all_messages[pivot_index:]
    )
    if not to_summarize:
        side = "before" if direction == "up_to" else "after"
        raise ValueError(f"Nothing to summarize {side} the selected message.")
    if direction == "up_to":
        to_keep = tuple(
            message
            for message in all_messages[pivot_index:]
            if not _extra(message, "compact_boundary")
            and not _extra(message, "is_compact_summary")
            and message.role != "progress"
        )
        api_messages = to_summarize
    else:
        to_keep = tuple(
            message
            for message in all_messages[:pivot_index]
            if message.role != "progress"
        )
        api_messages = all_messages
    custom = f"User context: {user_feedback}" if user_feedback else None
    prompt = get_partial_compact_prompt(custom, direction=direction)
    summary, call_tokens, _ = await _compact_summary(
        api_messages, prompt=prompt, context=context
    )
    return _result(
        all_messages=all_messages,
        covered_messages=to_summarize,
        raw_summary=summary,
        compact_call_tokens=call_tokens,
        messages_to_keep=to_keep,
        prompt=prompt,
        trigger="manual",
        context=context,
    )


__all__ = [
    "COMPACT_MAX_OUTPUT_TOKENS",
    "MAX_PTL_RETRIES",
    "PTL_RETRY_MARKER",
    "TraditionalCompactContext",
    "build_post_compact_messages",
    "compact_conversation",
    "partial_compact_conversation",
    "truncate_head_for_ptl_retry",
]

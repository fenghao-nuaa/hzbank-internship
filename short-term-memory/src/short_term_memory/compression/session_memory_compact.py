"""Claude L4 Session Memory compact fast path."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import uuid

from short_term_memory.compression.context_messages import (
    annotate_active_message,
    to_provider_messages,
)
from short_term_memory.compression.compact_prompt import (
    get_compact_user_summary_message,
)
from short_term_memory.compression.message_rounds import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_TEXT_BLOCK_MESSAGES,
    DEFAULT_MIN_TOKENS,
    TokenEstimator,
    calculate_messages_to_keep_index,
)
from short_term_memory.compression.session_memory_prompt import (
    EMPTY_SESSION_MEMORY,
    truncate_session_memory_for_compact,
)
from short_term_memory.models import (
    CompactBoundary,
    ContextRevision,
    MemoryEvent,
    SessionCompressionMessage,
    SessionMemoryRevision,
)

SM_MIN_TOKENS = DEFAULT_MIN_TOKENS
SM_MIN_TEXT_MESSAGES = DEFAULT_MIN_TEXT_BLOCK_MESSAGES
SM_MAX_TOKENS = DEFAULT_MAX_TOKENS
SM_WAIT_SECONDS = 15.0
SM_STALE_SECONDS = 60.0

ExtractionWaiter = Callable[[], Awaitable[SessionMemoryRevision | None]]


@dataclass(frozen=True)
class CompactionResult:
    boundary_marker: SessionCompressionMessage
    summary_messages: tuple[SessionCompressionMessage, ...]
    messages_to_keep: tuple[SessionCompressionMessage, ...]
    attachments: tuple[SessionCompressionMessage, ...] = ()
    hook_results: tuple[SessionCompressionMessage, ...] = ()
    pre_compact_token_count: int = 0
    post_compact_token_count: int = 0
    true_post_compact_token_count: int = 0
    compact_prompt: str = ""


@dataclass(frozen=True)
class SessionMemoryCompactContext:
    token_estimator: TokenEstimator
    history_turns: int
    auto_compact_threshold: int | None = None
    extraction_started_at: datetime | None = None
    extraction_waiter: ExtractionWaiter | None = None
    attachments: tuple[SessionCompressionMessage, ...] = field(default_factory=tuple)
    hook_results: tuple[SessionCompressionMessage, ...] = field(default_factory=tuple)
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if self.history_turns < 1:
            raise ValueError("history_turns must be positive")
        if self.auto_compact_threshold is not None and self.auto_compact_threshold < 1:
            raise ValueError("auto_compact_threshold must be positive")

    async def wait_for_current_extraction(
        self,
        session_memory: SessionMemoryRevision | None,
        *,
        timeout_seconds: float,
        stale_seconds: float,
    ) -> SessionMemoryRevision | None:
        if self.extraction_waiter is None or self.extraction_started_at is None:
            return session_memory
        now = self.clock()
        for timestamp in (now, self.extraction_started_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("extraction timestamps must be timezone-aware")
        if (now - self.extraction_started_at).total_seconds() > stale_seconds:
            return session_memory
        return await asyncio.wait_for(
            self.extraction_waiter(), timeout=timeout_seconds
        )


def _sequence_through(message: SessionCompressionMessage) -> int | None:
    value = (message.model_extra or {}).get("stm_sequence_through")
    return value if isinstance(value, int) else None


def find_coverage_index(
    messages: tuple[SessionCompressionMessage, ...], covered_through_sequence: int
) -> int | None:
    """Translate Claude's exact summarized-message UUID lookup to sequence metadata."""

    return next(
        (
            index
            for index, message in enumerate(messages)
            if _sequence_through(message) == covered_through_sequence
        ),
        None,
    )


def _is_boundary(message: SessionCompressionMessage) -> bool:
    return bool((message.model_extra or {}).get("compact_boundary"))


def build_post_compact_messages(
    result: CompactionResult,
) -> tuple[SessionCompressionMessage, ...]:
    """Replace activity context in the exact order used by Claude compact."""

    return (
        result.boundary_marker,
        *result.summary_messages,
        *result.messages_to_keep,
        *result.attachments,
        *result.hook_results,
    )


def _boundary_message(
    memory: SessionMemoryRevision,
    *,
    pre_compact_tokens: int,
    true_post_compact_tokens: int,
    created_at: datetime,
) -> SessionCompressionMessage:
    boundary = CompactBoundary(
        boundary_id=uuid.uuid4().hex,
        trigger="auto",
        strategy="session_memory",
        covered_through_sequence=memory.covered_through_sequence,
        pre_compact_tokens=pre_compact_tokens,
        true_post_compact_tokens=true_post_compact_tokens,
        created_at=created_at.isoformat(),
    )
    return SessionCompressionMessage(
        role="system",
        content="[compact boundary]",
        compact_boundary=boundary.model_dump(mode="json"),
    )


def _create_result(
    *,
    messages: tuple[SessionCompressionMessage, ...],
    memory: SessionMemoryRevision,
    messages_to_keep: tuple[SessionCompressionMessage, ...],
    context: SessionMemoryCompactContext,
) -> CompactionResult:
    pre = context.token_estimator.estimate(to_provider_messages(messages))
    boundary = _boundary_message(
        memory,
        pre_compact_tokens=pre,
        true_post_compact_tokens=0,
        created_at=context.clock(),
    )
    compact_memory, was_truncated = truncate_session_memory_for_compact(
        memory.content
    )
    summary = get_compact_user_summary_message(
        compact_memory,
        suppress_follow_up_questions=True,
        recent_messages_preserved=True,
    )
    if was_truncated and isinstance(summary.content, str):
        summary = summary.model_copy(
            update={
                "content": summary.content
                + "\n\nSome session memory sections were truncated for length. "
                "The full session memory can be viewed at: "
                "redis://current-session/session-memory"
            }
        )
    return CompactionResult(
        boundary_marker=boundary,
        summary_messages=(summary,),
        messages_to_keep=messages_to_keep,
        attachments=context.attachments,
        hook_results=context.hook_results,
        pre_compact_token_count=pre,
        post_compact_token_count=context.token_estimator.estimate(
            to_provider_messages((summary,))
        ),
    )


def materialize_session_memory_recovery_revision(
    *,
    session_memory: SessionMemoryRevision,
    recent_originals: tuple[MemoryEvent, ...],
    now: datetime,
) -> ContextRevision:
    """Materialize Claude's L4 compact projection without another model call."""

    keep = tuple(
        annotate_active_message(
            SessionCompressionMessage(
                role=event.role.value,
                content=event.content,
                stm_timestamp=event.created_at,
            ),
            from_sequence=event.sequence,
            through_sequence=event.sequence,
            group_id=f"event:{event.sequence}",
        )
        for event in recent_originals
        if event.sequence > session_memory.covered_through_sequence
    )
    boundary = CompactBoundary(
        boundary_id=(
            f"recovery:{session_memory.version}:"
            f"{session_memory.covered_through_sequence}"
        ),
        trigger="reactive",
        strategy="session_memory",
        covered_through_sequence=session_memory.covered_through_sequence,
        pre_compact_tokens=session_memory.token_count,
        true_post_compact_tokens=session_memory.token_count,
        created_at=now.isoformat(),
    )
    return ContextRevision(
        version=1,
        boundary=boundary,
        summary_message=get_compact_user_summary_message(
            session_memory.content,
            suppress_follow_up_questions=True,
            recent_messages_preserved=bool(keep),
        ),
        messages_to_keep=keep,
        updated_at=now.isoformat(),
    )


async def try_session_memory_compaction(
    *,
    messages: tuple[SessionCompressionMessage, ...],
    session_memory: SessionMemoryRevision | None,
    context: SessionMemoryCompactContext,
) -> CompactionResult | None:
    """Return L4 compact output, or ``None`` for every Claude fallback path."""

    try:
        memory = await context.wait_for_current_extraction(
            session_memory,
            timeout_seconds=SM_WAIT_SECONDS,
            stale_seconds=SM_STALE_SECONDS,
        )
        if memory is None or memory.content.strip() == EMPTY_SESSION_MEMORY.strip():
            return None
        coverage_index = find_coverage_index(
            messages, memory.covered_through_sequence
        )
        if coverage_index is None:
            return None
        start = calculate_messages_to_keep_index(
            messages,
            coverage_index,
            context.token_estimator,
            recent_user_turns=context.history_turns,
            min_tokens=SM_MIN_TOKENS,
            min_text_messages=SM_MIN_TEXT_MESSAGES,
            max_tokens=SM_MAX_TOKENS,
        )
        messages_to_keep = tuple(
            message for message in messages[start:] if not _is_boundary(message)
        )
        result = _create_result(
            messages=messages,
            memory=memory,
            messages_to_keep=messages_to_keep,
            context=context,
        )
        post = context.token_estimator.estimate(
            to_provider_messages(build_post_compact_messages(result))
        )
        threshold = context.auto_compact_threshold
        if threshold is not None and post >= threshold:
            return None
        boundary = _boundary_message(
            memory,
            pre_compact_tokens=result.pre_compact_token_count,
            true_post_compact_tokens=post,
            created_at=context.clock(),
        )
        return replace(
            result,
            boundary_marker=boundary,
            post_compact_token_count=post,
            true_post_compact_token_count=post,
        )
    except Exception:
        return None

"""Claude L2 Auto Compact threshold and L4-to-L3 dispatcher."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import uuid

from short_term_memory.compression.context_messages import to_provider_messages
from short_term_memory.compression.message_rounds import TokenEstimator
from short_term_memory.compression.session_memory_compact import CompactionResult
from short_term_memory.models import (
    AutoCompactTrackingState,
    SessionCompressionMessage,
)

MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

L4Compactor = Callable[
    [tuple[SessionCompressionMessage, ...], int],
    Awaitable[CompactionResult | None],
]
L3Compactor = Callable[
    [tuple[SessionCompressionMessage, ...], AutoCompactTrackingState],
    Awaitable[CompactionResult],
]


@dataclass(frozen=True)
class ModelProfile:
    context_window_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if min(self.context_window_tokens, self.max_output_tokens) < 1:
            raise ValueError("model token limits must be positive")
        if self.max_output_tokens >= self.context_window_tokens:
            raise ValueError("max output must be smaller than context window")


@dataclass(frozen=True)
class AutoCompactContext:
    model_profile: ModelProfile
    token_estimator: TokenEstimator
    query_source: str
    try_session_memory: L4Compactor
    compact_conversation: L3Compactor


@dataclass(frozen=True)
class AutoCompactResult:
    was_compacted: bool
    tracking: AutoCompactTrackingState
    compaction_result: CompactionResult | None = None


def effective_context_window(profile: ModelProfile) -> int:
    return profile.context_window_tokens - min(
        profile.max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY
    )


def auto_compact_threshold(profile: ModelProfile) -> int:
    return effective_context_window(profile) - AUTOCOMPACT_BUFFER_TOKENS


def manual_compact_threshold(profile: ModelProfile) -> int:
    return effective_context_window(profile) - MANUAL_COMPACT_BUFFER_TOKENS


def _not_compacted(tracking: AutoCompactTrackingState) -> AutoCompactResult:
    return AutoCompactResult(was_compacted=False, tracking=tracking)


async def auto_compact_if_needed(
    *,
    messages: tuple[SessionCompressionMessage, ...],
    context: AutoCompactContext,
    tracking: AutoCompactTrackingState,
    manual: bool = False,
) -> AutoCompactResult:
    """Try L4, then L3, preserving Claude's guards and failure breaker."""

    if context.query_source in {"session_memory", "compact"}:
        return _not_compacted(tracking)
    if (
        not manual
        and tracking.consecutive_failures
        >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
    ):
        return _not_compacted(tracking)
    threshold = (
        manual_compact_threshold(context.model_profile)
        if manual
        else auto_compact_threshold(context.model_profile)
    )
    current_tokens = context.token_estimator.estimate(
        to_provider_messages(messages)
    )
    if current_tokens < threshold:
        return _not_compacted(tracking)

    l4 = await context.try_session_memory(messages, threshold)
    if l4 is not None and l4.true_post_compact_token_count < threshold:
        return AutoCompactResult(
            was_compacted=True,
            compaction_result=l4,
            tracking=tracking.reset_success(uuid.uuid4().hex),
        )
    try:
        l3 = await context.compact_conversation(messages, tracking)
    except Exception:
        return _not_compacted(tracking.record_failure())
    return AutoCompactResult(
        was_compacted=True,
        compaction_result=l3,
        tracking=tracking.reset_success(uuid.uuid4().hex),
    )

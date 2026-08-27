"""Pure Claude Session Memory thresholds and extraction timing predicates."""

from dataclasses import dataclass, fields
from datetime import datetime, timezone


@dataclass(frozen=True)
class SessionMemoryConfig:
    minimum_message_tokens_to_init: int = 10_000
    minimum_tokens_between_update: int = 5_000
    tool_calls_between_updates: int = 3
    wait_for_extraction_seconds: float = 15.0
    stale_extraction_seconds: float = 60.0
    max_section_tokens: int = 2_000
    max_total_tokens: int = 12_000

    def __post_init__(self) -> None:
        for field in fields(self):
            if getattr(self, field.name) <= 0:
                raise ValueError(f"{field.name} must be positive")


DEFAULT_SESSION_MEMORY_CONFIG = SessionMemoryConfig()


def should_extract_memory_from_counts(
    *,
    token_growth_reached: bool,
    tool_calls_since_update: int,
    last_assistant_turn_has_tool_calls: bool,
    config: SessionMemoryConfig = DEFAULT_SESSION_MEMORY_CONFIG,
) -> bool:
    """Direct translation of ``sessionMemory.ts:shouldExtractMemory``."""

    tool_threshold_reached = (
        tool_calls_since_update >= config.tool_calls_between_updates
    )
    return (
        token_growth_reached and tool_threshold_reached
    ) or (
        token_growth_reached and not last_assistant_turn_has_tool_calls
    )


def should_extract_memory(
    *,
    current_token_count: int,
    tokens_at_last_extraction: int,
    tool_calls_since_update: int,
    last_assistant_turn_has_tool_calls: bool,
    initialized: bool,
    config: SessionMemoryConfig = DEFAULT_SESSION_MEMORY_CONFIG,
) -> bool:
    """Apply initialization and context-growth thresholds before the truth table."""

    if current_token_count < 0 or tokens_at_last_extraction < 0:
        raise ValueError("token counts must be non-negative")
    if tool_calls_since_update < 0:
        raise ValueError("tool_calls_since_update must be non-negative")
    if (
        not initialized
        and current_token_count < config.minimum_message_tokens_to_init
    ):
        return False
    token_growth_reached = (
        current_token_count - tokens_at_last_extraction
        >= config.minimum_tokens_between_update
    )
    return should_extract_memory_from_counts(
        token_growth_reached=token_growth_reached,
        tool_calls_since_update=tool_calls_since_update,
        last_assistant_turn_has_tool_calls=last_assistant_turn_has_tool_calls,
        config=config,
    )


def is_extraction_stale(
    extraction_started_at: datetime | None,
    *,
    now: datetime | None = None,
    config: SessionMemoryConfig = DEFAULT_SESSION_MEMORY_CONFIG,
) -> bool:
    """Return true only when an extraction is older than Claude's 60s limit."""

    if extraction_started_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    for value in (extraction_started_at, current):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("extraction timestamps must be timezone-aware")
    return (
        current - extraction_started_at
    ).total_seconds() > config.stale_extraction_seconds

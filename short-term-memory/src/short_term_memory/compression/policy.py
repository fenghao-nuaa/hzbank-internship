"""PLAN-defined threshold policy for background compression."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HeadroomPolicy:
    context_window_tokens: int
    trigger_ratio: float
    max_messages: int
    max_session_seconds: int

    def __post_init__(self) -> None:
        if not 0.60 <= self.trigger_ratio <= 0.70:
            raise ValueError("trigger_ratio must be between 0.60 and 0.70")
        if self.context_window_tokens < 1:
            raise ValueError("context_window_tokens must be positive")
        if self.max_messages < 1:
            raise ValueError("max_messages must be positive")
        if self.max_session_seconds < 1:
            raise ValueError("max_session_seconds must be positive")

    def should_compress(
        self,
        *,
        estimated_tokens: int,
        message_count: int,
        session_seconds: int,
    ) -> bool:
        return (
            estimated_tokens >= self.context_window_tokens * self.trigger_ratio
            or message_count >= self.max_messages
            or session_seconds >= self.max_session_seconds
        )

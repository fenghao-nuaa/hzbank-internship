"""Replaceable ports used by the short-term memory SDK."""

from typing import Any, Literal, Protocol

from short_term_memory.models import (
    EventReservation,
    MemoryEvent,
    MemorySummaryEnvelope,
)


class RebuildCompletionWaiter(Protocol):
    """Worker-service completion boundary for cold CCR rebuilds."""

    async def wait_for(
        self, job: Any, timeout_seconds: float
    ) -> MemorySummaryEnvelope | None: ...


class AsyncMemoryStore(Protocol):
    """Async storage boundary for sequence-aware memory events."""

    async def reserve_event(
        self, user_id: str, session_id: str, event_id: str, digest: str
    ) -> EventReservation: ...

    async def commit_event(
        self, user_id: str, session_id: str, event: MemoryEvent
    ) -> Literal["committed", "duplicate"]: ...

    async def read_recent_originals(
        self, user_id: str, session_id: str, history_turns: int
    ) -> tuple[MemoryEvent, ...]: ...

    async def read_latest_sequence(self, user_id: str, session_id: str) -> int: ...

    async def read_envelope(
        self, user_id: str, session_id: str
    ) -> MemorySummaryEnvelope | None: ...

    async def compare_and_set_envelope(
        self,
        user_id: str,
        session_id: str,
        expected_version: int,
        envelope: MemorySummaryEnvelope,
    ) -> bool: ...

    async def acquire_session_memory_extraction(
        self,
        user_id: str,
        session_id: str,
        token: str,
        *,
        expected_version: int,
        started_at: str,
    ) -> bool: ...

    async def read_session_memory_extraction(
        self, user_id: str, session_id: str
    ) -> Any | None: ...

    async def release_session_memory_extraction(
        self, user_id: str, session_id: str, token: str
    ) -> bool: ...

    async def restore_originals(
        self,
        user_id: str,
        session_id: str,
        originals: tuple[MemoryEvent, ...],
    ) -> bool: ...

    async def restore_session_projection(
        self,
        user_id: str,
        session_id: str,
        *,
        latest_sequence: int,
        originals: tuple[MemoryEvent, ...],
        envelope: MemorySummaryEnvelope | None,
    ) -> bool: ...

    async def acquire_session_activation_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool: ...

    async def release_session_activation_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool: ...

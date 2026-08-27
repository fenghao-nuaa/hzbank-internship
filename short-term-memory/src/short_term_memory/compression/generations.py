"""Original-only compression planning and read-time generation assembly."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from short_term_memory.compression.context_messages import to_provider_messages
from short_term_memory.compression.context_query import load_active_messages
from short_term_memory.models import MemoryEvent, MemorySummaryEnvelope
from short_term_memory.storage.journal_store import JournalStore


class OriginalSequenceGapError(ValueError):
    """A requested journal-original range has an unfilled sequence gap."""


class OriginalSequenceConflictError(ValueError):
    """Two different original events claim the same sequence."""


class OriginalMemoryStore(Protocol):
    async def read_originals_after(
        self, user_id: str, session_id: str, sequence: int
    ) -> tuple[MemoryEvent, ...]: ...

    async def read_envelope(
        self, user_id: str, session_id: str
    ) -> MemorySummaryEnvelope | None: ...


@dataclass(frozen=True)
class CompressionCandidate:
    """A CAS-bound range of journal-original events for one compression call."""

    user_id: str
    session_id: str
    expected_version: int
    from_sequence: int
    through_sequence: int
    originals: tuple[MemoryEvent, ...]
    rebuild: bool


class GenerationPlanner:
    """Choose source ranges without ever consulting prior Headroom output."""

    def __init__(
        self, store: OriginalMemoryStore, journals: JournalStore, *, max_segments: int
    ) -> None:
        if max_segments < 1:
            raise ValueError("max_segments must be positive")
        self.store = store
        self.journals = journals
        self.max_segments = max_segments

    async def plan_incremental(
        self, user_id: str, session_id: str
    ) -> CompressionCandidate | None:
        envelope = await self.store.read_envelope(user_id, session_id)
        through = envelope.compressed_through_sequence if envelope else 0
        originals = self._contiguous_prefix(
            through + 1,
            self._deduplicate_by_sequence(
                await self.store.read_originals_after(user_id, session_id, through)
            ),
        )
        if not originals:
            return None
        return CompressionCandidate(
            user_id=user_id,
            session_id=session_id,
            expected_version=envelope.version if envelope else 0,
            from_sequence=originals[0].sequence,
            through_sequence=originals[-1].sequence,
            originals=originals,
            rebuild=False,
        )

    async def plan_rebuild(
        self, user_id: str, session_id: str, through_sequence: int
    ) -> CompressionCandidate | None:
        if through_sequence < 1:
            raise ValueError("through_sequence must be positive")
        envelope = await self.store.read_envelope(user_id, session_id)
        originals = self._complete_range(
            through_sequence,
            self._deduplicate_by_sequence(
                self.journals.read_original_range(
                    user_id, session_id, 1, through_sequence
                )
            ),
        )
        return CompressionCandidate(
            user_id=user_id,
            session_id=session_id,
            expected_version=envelope.version if envelope else 0,
            from_sequence=originals[0].sequence,
            through_sequence=originals[-1].sequence,
            originals=originals,
            rebuild=True,
        )

    @staticmethod
    def _deduplicate_by_sequence(
        originals: tuple[MemoryEvent, ...]
    ) -> tuple[MemoryEvent, ...]:
        unique: dict[int, MemoryEvent] = {}
        for event in originals:
            existing = unique.get(event.sequence)
            if existing is not None and existing != event:
                raise OriginalSequenceConflictError(
                    "conflicting original events for sequence "
                    f"{event.sequence}"
                )
            unique.setdefault(event.sequence, event)
        return tuple(unique[sequence] for sequence in sorted(unique))

    @staticmethod
    def _contiguous_prefix(
        first_sequence: int, originals: tuple[MemoryEvent, ...]
    ) -> tuple[MemoryEvent, ...]:
        contiguous: list[MemoryEvent] = []
        expected = first_sequence
        for event in originals:
            if event.sequence < expected:
                continue
            if event.sequence != expected:
                break
            contiguous.append(event)
            expected += 1
        return tuple(contiguous)

    @staticmethod
    def _complete_range(
        through_sequence: int, originals: tuple[MemoryEvent, ...]
    ) -> tuple[MemoryEvent, ...]:
        expected = 1
        for event in originals:
            if event.sequence != expected:
                raise OriginalSequenceGapError(f"missing sequence {expected}")
            expected += 1
        if expected <= through_sequence:
            raise OriginalSequenceGapError(f"missing sequence {expected}")
        return originals


class GenerationAssembler:
    """Assemble a read context while preserving opaque generation messages."""

    def __init__(self, *, max_segments: int) -> None:
        if max_segments < 1:
            raise ValueError("max_segments must be positive")
        self.max_segments = max_segments

    def build_read_messages(
        self,
        envelope: MemorySummaryEnvelope | None,
        recent_originals: tuple[MemoryEvent, ...],
        now: datetime,
    ) -> tuple[dict[str, Any], ...]:
        return to_provider_messages(
            load_active_messages(
                envelope,
                recent_originals,
                now,
                max_segments=self.max_segments,
            )
        )

    def _fresh_generations(
        self, envelope: MemorySummaryEnvelope, now: datetime
    ) -> tuple:
        fresh = tuple(
            generation
            for generation in envelope.compression_generations
            if self._expires_at(generation.ccr_expires_at) > now
        )
        return fresh[-self.max_segments :]

    @staticmethod
    def _expires_at(value: str) -> datetime:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("ccr_expires_at must be timezone-aware")
        return expires_at

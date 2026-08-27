"""Assemble the single active context after the latest compact boundary."""

from datetime import datetime
import json

from short_term_memory.compression.context_messages import annotate_active_message
from short_term_memory.compression.session_memory_compact import (
    CompactionResult,
    build_post_compact_messages,
)
from short_term_memory.models import (
    CompactBoundary,
    CompressionGeneration,
    MemoryEvent,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
)


def _expires_at(value: str) -> datetime:
    expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("ccr_expires_at must be timezone-aware")
    return expires_at


def generation_is_visible(
    generation: CompressionGeneration,
    boundary: CompactBoundary | None,
) -> bool:
    return boundary is None or (
        generation.through_sequence > boundary.covered_through_sequence
    )


def _boundary_message(boundary: CompactBoundary) -> SessionCompressionMessage:
    return SessionCompressionMessage(
        role="system",
        content="[compact boundary]",
        compact_boundary=boundary.model_dump(mode="json"),
        stm_sequence_from=0,
        stm_sequence_through=boundary.covered_through_sequence,
        stm_group_id=f"revision:{boundary.boundary_id}",
    )


def _revision_summary(
    boundary: CompactBoundary, message: SessionCompressionMessage
) -> SessionCompressionMessage:
    return SessionCompressionMessage.model_validate(
        {
            **message.model_dump(mode="json"),
            "stm_sequence_from": 0,
            "stm_sequence_through": boundary.covered_through_sequence,
            "stm_group_id": f"revision:{boundary.boundary_id}",
        }
    )


def _event_message(event: MemoryEvent) -> SessionCompressionMessage:
    return annotate_active_message(
        SessionCompressionMessage(
            role=event.role.value,
            content=event.content,
            stm_timestamp=event.created_at,
        ),
        from_sequence=event.sequence,
        through_sequence=event.sequence,
        group_id=f"event:{event.sequence}",
    )


def _deduplicate(
    messages: tuple[SessionCompressionMessage, ...],
) -> tuple[SessionCompressionMessage, ...]:
    seen: set[tuple[object, ...]] = set()
    result: list[SessionCompressionMessage] = []
    for message in messages:
        extra = message.model_extra or {}
        identity = (
            extra.get("stm_sequence_from"),
            extra.get("stm_sequence_through"),
            message.role,
            json.dumps(message.model_dump(mode="json"), sort_keys=True),
        )
        if identity not in seen:
            seen.add(identity)
            result.append(message)
    return tuple(result)


def load_active_messages(
    envelope: MemorySummaryEnvelope | None,
    recent_originals: tuple[MemoryEvent, ...],
    now: datetime,
    *,
    max_segments: int | None = None,
) -> tuple[SessionCompressionMessage, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if max_segments is not None and max_segments < 1:
        raise ValueError("max_segments must be positive")
    result: list[SessionCompressionMessage] = []
    boundary: CompactBoundary | None = None
    if envelope is not None and envelope.active_revision is not None:
        revision = envelope.active_revision
        boundary = revision.boundary
        result.extend(
            (_boundary_message(boundary), _revision_summary(boundary, revision.summary_message))
        )
    if envelope is not None:
        generations = tuple(
            item
            for item in envelope.compression_generations
            if _expires_at(item.ccr_expires_at) > now
            and generation_is_visible(item, boundary)
        )
        if max_segments is not None:
            generations = generations[-max_segments:]
        for generation in generations:
            result.extend(
                annotate_active_message(
                    message,
                    from_sequence=generation.from_sequence,
                    through_sequence=generation.through_sequence,
                    group_id=f"generation:{generation.generation}",
                )
                for message in generation.messages
            )
    if envelope is not None and envelope.active_revision is not None:
        result.extend(envelope.active_revision.messages_to_keep)
    covered = boundary.covered_through_sequence if boundary else 0
    result.extend(
        _event_message(event)
        for event in recent_originals
        if event.sequence > covered
    )
    return _deduplicate(tuple(result))


def apply_compaction_result(
    current_messages: tuple[SessionCompressionMessage, ...],
    result: CompactionResult,
) -> tuple[SessionCompressionMessage, ...]:
    """Replace, never append to, the prior active compact context."""

    del current_messages
    return build_post_compact_messages(result)

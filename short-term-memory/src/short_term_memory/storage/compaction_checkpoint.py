"""Immutable Journal checkpoints for Claude L3/L4 continuity state."""

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from short_term_memory.models import (
    AutoCompactTrackingState,
    ContextRevision,
    MemorySummaryEnvelope,
    SessionMemoryRevision,
)


class CompactionCheckpoint(BaseModel):
    """A durable L3/L4 projection; Headroom generations stay outside Journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["compaction_checkpoint"] = "compaction_checkpoint"
    schema_version: Literal[1] = 1
    checkpoint_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    envelope_version: int = Field(ge=1)
    compressed_through_sequence: int = Field(ge=0)
    generation_versions: tuple[int, ...] = ()
    session_memory: SessionMemoryRevision | None = None
    active_revision: ContextRevision | None = None
    auto_compact_tracking: AutoCompactTrackingState
    created_at: str = Field(min_length=1)

    @field_validator("generation_versions")
    @classmethod
    def positive_generation_versions(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value < 1 for value in values):
            raise ValueError("generation versions must be positive")
        return values


def checkpoint_from_envelope(
    user_id: str,
    session_id: str,
    envelope: MemorySummaryEnvelope,
) -> CompactionCheckpoint:
    """Snapshot only the durable Claude continuity fields from an envelope."""

    state = {
        "user_id": user_id,
        "session_id": session_id,
        "envelope_version": envelope.version,
        "compressed_through_sequence": envelope.compressed_through_sequence,
        "generation_versions": tuple(
            generation.generation for generation in envelope.compression_generations
        ),
        "session_memory": envelope.session_memory,
        "active_revision": envelope.active_revision,
        "auto_compact_tracking": envelope.auto_compact_tracking,
        "created_at": envelope.updated_at,
    }
    canonical_state = {
        **state,
        "session_memory": (
            envelope.session_memory.model_dump(mode="json")
            if envelope.session_memory
            else None
        ),
        "active_revision": (
            envelope.active_revision.model_dump(mode="json")
            if envelope.active_revision
            else None
        ),
        "auto_compact_tracking": envelope.auto_compact_tracking.model_dump(
            mode="json"
        ),
    }
    canonical = json.dumps(
        canonical_state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CompactionCheckpoint(
        checkpoint_id=f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}",
        **state,
    )


def checkpoint_to_envelope(
    checkpoint: CompactionCheckpoint,
) -> MemorySummaryEnvelope:
    """Restore an online envelope without reviving Headroom messages or markers."""

    return MemorySummaryEnvelope(
        version=checkpoint.envelope_version,
        compressed_through_sequence=checkpoint.compressed_through_sequence,
        compression_generations=(),
        session_memory=checkpoint.session_memory,
        active_revision=checkpoint.active_revision,
        auto_compact_tracking=checkpoint.auto_compact_tracking,
        updated_at=checkpoint.created_at,
    )

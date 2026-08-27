from datetime import datetime, timezone

from short_term_memory.models import (
    CompactBoundary,
    CompressionGeneration,
    ContextRevision,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
    SessionMemoryRevision,
)
from short_term_memory.storage.compaction_checkpoint import (
    checkpoint_from_envelope,
    checkpoint_to_envelope,
)


NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


def envelope_with_l3_l4_and_generation() -> MemorySummaryEnvelope:
    boundary = CompactBoundary(
        boundary_id="boundary-1",
        trigger="auto",
        strategy="session_memory",
        covered_through_sequence=80,
        pre_compact_tokens=10_000,
        true_post_compact_tokens=1_000,
        created_at=NOW.isoformat(),
    )
    active_revision = ContextRevision(
        version=3,
        boundary=boundary,
        summary_message=SessionCompressionMessage(
            role="user", content="continuity summary", is_compact_summary=True
        ),
        messages_to_keep=(
            SessionCompressionMessage(role="user", content="recent question"),
        ),
        covered_generation_ids=(1,),
        updated_at=NOW.isoformat(),
    )
    session_memory = SessionMemoryRevision(
        version=2,
        content="session memory",
        covered_through_sequence=80,
        token_count=500,
        updated_at=NOW.isoformat(),
    )
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=80,
        messages=(
            SessionCompressionMessage(
                role="assistant", content="compressed hash=abc123"
            ),
        ),
        tokens_before=10_000,
        tokens_after=2_000,
        created_at=NOW.isoformat(),
        ccr_expires_at=NOW.replace(hour=13).isoformat(),
    )
    return MemorySummaryEnvelope(
        version=7,
        compressed_through_sequence=80,
        compression_generations=(generation,),
        session_memory=session_memory,
        active_revision=active_revision,
        updated_at=NOW.isoformat(),
    )


def test_checkpoint_id_is_deterministic_and_does_not_store_headroom_messages() -> None:
    envelope = envelope_with_l3_l4_and_generation()

    first = checkpoint_from_envelope("u", "s", envelope)
    second = checkpoint_from_envelope("u", "s", envelope)

    assert first.checkpoint_id == second.checkpoint_id
    assert first.generation_versions == (1,)
    assert "compression_generations" not in first.model_dump()
    assert "compressed hash=abc123" not in first.model_dump_json()


def test_checkpoint_restores_only_l3_l4_projection() -> None:
    checkpoint = checkpoint_from_envelope(
        "u", "s", envelope_with_l3_l4_and_generation()
    )

    restored = checkpoint_to_envelope(checkpoint)

    assert restored.active_revision == checkpoint.active_revision
    assert restored.session_memory == checkpoint.session_memory
    assert restored.compression_generations == ()
    assert (
        restored.compressed_through_sequence
        == checkpoint.compressed_through_sequence
    )
    assert restored.version == checkpoint.envelope_version

import pytest

from short_term_memory.models import (
    AutoCompactTrackingState,
    CompactBoundary,
    CompressionGeneration,
    ContextRevision,
    MemoryContentType,
    MemoryEvent,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
    SessionMemoryRevision,
)


def test_memory_event_rejects_unknown_fields_and_invalid_digest() -> None:
    event = MemoryEvent(
        sequence=1,
        event_id="event-1",
        role="user",
        content_type=MemoryContentType.CODE,
        content="print('ok')",
        metadata={"language": "python"},
        sha256="a" * 64,
        created_at="2026-08-06T00:00:00+00:00",
    )

    assert event.content_type is MemoryContentType.CODE
    with pytest.raises(ValueError):
        MemoryEvent.model_validate({**event.model_dump(), "unexpected": True})
    with pytest.raises(ValueError):
        MemoryEvent.model_validate({**event.model_dump(), "sha256": "bad"})


def test_compression_generation_requires_an_ordered_sequence_range() -> None:
    with pytest.raises(ValueError, match="through_sequence"):
        CompressionGeneration(
            generation=1,
            from_sequence=2,
            through_sequence=1,
            messages=[SessionCompressionMessage(role="system", content="opaque")],
            tokens_before=10,
            tokens_after=4,
            created_at="2026-08-06T00:00:00+00:00",
            ccr_expires_at="2026-08-06T12:00:00+00:00",
        )


def test_memory_summary_envelope_v2_round_trips_compaction_state() -> None:
    boundary = CompactBoundary(
        boundary_id="boundary-1",
        trigger="auto",
        strategy="traditional",
        covered_through_sequence=8,
        pre_compact_tokens=100_000,
        true_post_compact_tokens=20_000,
        created_at="2026-08-06T00:00:00+00:00",
    )
    revision = ContextRevision(
        version=1,
        boundary=boundary,
        summary_message=SessionCompressionMessage(role="user", content="summary"),
        messages_to_keep=(),
        covered_generation_ids=(1, 2),
        updated_at="2026-08-06T00:00:00+00:00",
    )
    envelope = MemorySummaryEnvelope(
        schema_version=2,
        version=1,
        compressed_through_sequence=4,
        compression_generations=[],
        session_memory=SessionMemoryRevision(
            version=1,
            content="# Session Title\nCompaction",
            covered_through_sequence=4,
            token_count=100,
            updated_at="2026-08-06T00:00:00+00:00",
        ),
        active_revision=revision,
        auto_compact_tracking=AutoCompactTrackingState(),
        updated_at="2026-08-06T00:00:00+00:00",
    )

    restored = MemorySummaryEnvelope.model_validate_json(envelope.model_dump_json())

    assert restored == envelope
    assert restored.schema_version == 2
    assert restored.active_revision is not None
    assert restored.active_revision.boundary.covered_through_sequence == 8


def test_memory_event_metadata_is_an_immutable_defensive_copy() -> None:
    metadata = {"language": "python"}
    event = MemoryEvent(
        sequence=1,
        event_id="event-1",
        role="user",
        content_type="code",
        content="print('ok')",
        metadata=metadata,
        sha256="a" * 64,
        created_at="2026-08-06T00:00:00+00:00",
    )

    metadata["language"] = "rust"

    assert event.metadata == {"language": "python"}
    with pytest.raises(TypeError):
        event.metadata["language"] = "go"


def test_compression_generation_messages_are_immutable() -> None:
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=[SessionCompressionMessage(role="system", content="opaque")],
        tokens_before=10,
        tokens_after=4,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )

    with pytest.raises(AttributeError):
        generation.messages.append(SessionCompressionMessage(role="user"))


def test_memory_summary_envelope_generations_are_immutable() -> None:
    envelope = MemorySummaryEnvelope(
        schema_version=2,
        version=1,
        compressed_through_sequence=0,
        compression_generations=[],
        auto_compact_tracking=AutoCompactTrackingState(),
        updated_at="2026-08-06T00:00:00+00:00",
    )

    with pytest.raises(AttributeError):
        envelope.compression_generations.append(object())


def test_memory_event_deep_copy_preserves_immutable_metadata() -> None:
    event = MemoryEvent(
        sequence=1,
        event_id="event-1",
        role="user",
        content_type="conversation",
        content="original",
        metadata={"source": "test"},
        sha256="a" * 64,
        created_at="2026-08-06T00:00:00+00:00",
    )

    copied = event.model_copy(deep=True)

    assert copied == event
    with pytest.raises(TypeError):
        copied.metadata["source"] = "changed"


def test_compression_generation_deeply_freezes_opaque_messages_and_round_trips() -> None:
    opaque_content = {"blocks": [{"anchors": ["opaque"]}]}
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=[SessionCompressionMessage(role="system", content=opaque_content)],
        tokens_before=10,
        tokens_after=4,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )

    opaque_content["blocks"][0]["anchors"].append("changed")

    with pytest.raises(ValueError):
        generation.messages[0].role = "user"
    with pytest.raises(AttributeError):
        generation.messages[0].content["blocks"][0]["anchors"].append("changed")
    dumped = generation.model_dump(mode="json")
    assert dumped["messages"] == [
        {"role": "system", "content": {"blocks": [{"anchors": ["opaque"]}]}}
    ]
    assert CompressionGeneration.model_validate(dumped) == generation


def test_session_compression_message_freezes_top_level_extra_fields_and_round_trips() -> None:
    message = SessionCompressionMessage(
        role="assistant",
        content="opaque",
        tool_calls=[{"id": "call-1", "arguments": {"query": "opaque"}}],
    )

    assert message.model_extra is not None
    with pytest.raises(TypeError):
        message.model_extra["tool_calls"] = []
    with pytest.raises(TypeError):
        message.model_extra["new_field"] = "opaque"
    with pytest.raises(TypeError):
        del message.model_extra["tool_calls"]

    dumped = message.model_dump(mode="json")
    assert dumped == {
        "role": "assistant",
        "content": "opaque",
        "tool_calls": [
            {"id": "call-1", "arguments": {"query": "opaque"}}
        ],
    }
    assert SessionCompressionMessage.model_validate(dumped) == message
    assert message.model_copy(deep=True) == message


def test_context_revision_collections_are_immutable_and_tracking_transitions() -> None:
    boundary = CompactBoundary(
        boundary_id="boundary-1",
        trigger="auto",
        strategy="session_memory",
        covered_through_sequence=3,
        pre_compact_tokens=10,
        true_post_compact_tokens=4,
        created_at="2026-08-06T00:00:00+00:00",
    )
    envelope = MemorySummaryEnvelope(
        schema_version=2,
        version=1,
        compressed_through_sequence=0,
        compression_generations=[],
        active_revision=ContextRevision(
            version=1,
            boundary=boundary,
            summary_message=SessionCompressionMessage(role="user", content="summary"),
            messages_to_keep=(SessionCompressionMessage(role="user", content="tail"),),
            covered_generation_ids=(1, 2),
            updated_at="2026-08-06T00:00:00+00:00",
        ),
        auto_compact_tracking=AutoCompactTrackingState(),
        updated_at="2026-08-06T00:00:00+00:00",
    )

    with pytest.raises(AttributeError):
        envelope.active_revision.covered_generation_ids.append(3)
    failed = envelope.auto_compact_tracking.record_failure()
    succeeded = failed.reset_success("turn-2")
    assert failed.consecutive_failures == 1
    assert succeeded == AutoCompactTrackingState(
        compacted=True, turn_counter=0, turn_id="turn-2", consecutive_failures=0
    )


def test_compact_boundary_rejects_negative_counts_and_revision_ids_are_positive() -> None:
    with pytest.raises(ValueError):
        CompactBoundary(
            boundary_id="b",
            trigger="auto",
            strategy="traditional",
            covered_through_sequence=0,
            pre_compact_tokens=-1,
            true_post_compact_tokens=0,
            created_at="2026-08-06T00:00:00+00:00",
        )

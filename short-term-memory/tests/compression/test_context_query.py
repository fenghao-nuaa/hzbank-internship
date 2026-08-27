from datetime import datetime, timedelta, timezone

from short_term_memory.compression.context_messages import to_provider_messages
from short_term_memory.compression.context_query import (
    apply_compaction_result,
    generation_is_visible,
    load_active_messages,
)
from short_term_memory.compression.session_memory_compact import CompactionResult
from short_term_memory.models import (
    CompactBoundary,
    CompressionGeneration,
    ContextRevision,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
)
from tests.factories import memory_event


NOW = datetime(2026, 8, 14, 11, 0, tzinfo=timezone.utc)


def generation(number, start, through, content, *, fresh=True):
    return CompressionGeneration(
        generation=number,
        from_sequence=start,
        through_sequence=through,
        messages=(SessionCompressionMessage(role="assistant", content=content, opaque="yes"),),
        tokens_before=100,
        tokens_after=50,
        created_at=NOW.isoformat(),
        ccr_expires_at=(NOW + (timedelta(hours=1) if fresh else -timedelta(seconds=1))).isoformat(),
    )


def boundary(through=10):
    return CompactBoundary(
        boundary_id="b1",
        trigger="auto",
        strategy="traditional",
        covered_through_sequence=through,
        pre_compact_tokens=100,
        true_post_compact_tokens=20,
        created_at=NOW.isoformat(),
    )


def envelope(*, generations=(), revision=None):
    return MemorySummaryEnvelope(
        version=1,
        compressed_through_sequence=max(
            (item.through_sequence for item in generations), default=0
        ),
        compression_generations=generations,
        active_revision=revision,
        updated_at=NOW.isoformat(),
    )


def active_revision(summary="AB", through=10):
    return ContextRevision(
        version=1,
        boundary=boundary(through),
        summary_message=SessionCompressionMessage(role="user", content=summary),
        messages_to_keep=(),
        updated_at=NOW.isoformat(),
    )


def test_no_revision_loads_fresh_generations_and_recent_originals() -> None:
    env = envelope(
        generations=(
            generation(1, 1, 2, "expired", fresh=False),
            generation(2, 3, 4, "fresh"),
        )
    )
    messages = load_active_messages(
        env, (memory_event(sequence=4, event_id="e4", content="recent"),), NOW
    )
    assert [item.content for item in messages] == ["fresh", "recent"]
    assert messages[0].model_extra["stm_group_id"] == "generation:2"
    assert messages[1].model_extra["stm_group_id"] == "event:4"
    assert "opaque" in to_provider_messages(messages)[0]
    assert all(not key.startswith("stm_") for item in to_provider_messages(messages) for key in item)


def test_revision_hides_covered_generation_and_original_but_keeps_newer_tail() -> None:
    env = envelope(
        generations=(
            generation(1, 1, 10, "covered"),
            generation(2, 8, 12, "partially newer"),
        ),
        revision=active_revision(through=10),
    )
    messages = load_active_messages(
        env,
        (
            memory_event(sequence=10, event_id="e10", content="old raw"),
            memory_event(sequence=13, event_id="e13", content="new raw"),
        ),
        NOW,
    )
    contents = [str(item.content) for item in messages]
    assert contents[:2] == ["[compact boundary]", "AB"]
    assert "covered" not in contents and "old raw" not in contents
    assert "partially newer" in contents and "new raw" in contents
    assert len(env.compression_generations) == 2


def test_late_covered_generation_stays_out_of_active_prompt() -> None:
    late = generation(2, 11, 20, "late")
    env = envelope(generations=(late,), revision=active_revision(through=20))
    assert not generation_is_visible(late, env.active_revision.boundary)
    assert "late" not in [str(item.content) for item in load_active_messages(env, (), NOW)]
    assert env.compression_generations == (late,)


def test_second_compact_replaces_first_summary_instead_of_appending() -> None:
    first = load_active_messages(envelope(revision=active_revision("AB")), (), NOW)
    new_summary = SessionCompressionMessage(role="user", content="ABCD")
    result = CompactionResult(
        boundary_marker=SessionCompressionMessage(
            role="system", content="new boundary", compact_boundary={"boundary_id": "b2"}
        ),
        summary_messages=(new_summary,),
        messages_to_keep=(),
    )
    second = apply_compaction_result(first, result)
    contents = [str(message.content) for message in second]
    assert "ABCD" in contents
    assert "AB" not in contents

from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from short_term_memory.compression.generations import (
    GenerationAssembler,
    GenerationPlanner,
    OriginalSequenceGapError,
)
from short_term_memory.models import CompressionGeneration
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.factories import envelope, memory_event
from tests.storage.fake_redis import AsyncFakeRedis


@pytest.fixture
def repository() -> AsyncRedisMemoryStore:
    return AsyncRedisMemoryStore(AsyncFakeRedis())


@pytest.fixture
def journals(tmp_path) -> JournalStore:
    return JournalStore(VFSAdapter(tmp_path))


async def seed_originals(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
    sequences: range,
) -> None:
    for sequence in sequences:
        content = f"original-{sequence}"
        digest = sha256(content.encode("utf-8")).hexdigest()
        reservation = await repository.reserve_event("u", "s", f"e-{sequence}", digest)
        assert reservation.sequence == sequence
        event = memory_event(
            sequence=reservation.sequence,
            event_id=f"e-{sequence}",
            content=content,
        )
        journals.append_event("u", "s", event)
        await repository.commit_event("u", "s", event)


class OriginalStoreWithEvents:
    def __init__(self, events):
        self.events = tuple(events)

    async def read_envelope(self, user_id: str, session_id: str):
        return None

    async def read_originals_after(self, user_id: str, session_id: str, sequence: int):
        return tuple(event for event in self.events if event.sequence > sequence)


def envelope_from(candidate, marker: str):
    generation = CompressionGeneration(
        generation=candidate.expected_version + 1,
        from_sequence=candidate.from_sequence,
        through_sequence=candidate.through_sequence,
        messages=[{"role": "system", "content": marker}],
        tokens_before=100,
        tokens_after=25,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )
    return envelope(
        version=candidate.expected_version + 1,
        through=candidate.through_sequence,
        generations=[generation],
    )


@pytest.mark.asyncio
async def test_later_generations_contain_only_new_original_events(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
) -> None:
    planner = GenerationPlanner(repository, journals, max_segments=8)
    await seed_originals(repository, journals, range(1, 101))
    first = await planner.plan_incremental("u", "s")
    assert first is not None
    assert await repository.compare_and_set_envelope(
        "u", "s", 0, envelope_from(first, marker="HR-1")
    )

    await seed_originals(repository, journals, range(101, 181))
    second = await planner.plan_incremental("u", "s")
    assert second is not None
    assert await repository.compare_and_set_envelope(
        "u", "s", 1, envelope_from(second, marker="HR-2")
    )

    await seed_originals(repository, journals, range(181, 241))
    third = await planner.plan_incremental("u", "s")
    assert third is not None

    assert [event.sequence for event in first.originals] == list(range(1, 101))
    assert [event.sequence for event in second.originals] == list(range(101, 181))
    assert [event.sequence for event in third.originals] == list(range(181, 241))
    rendered = json.dumps(
        [event.content for event in (*second.originals, *third.originals)]
    )
    assert "HR-1" not in rendered and "HR-2" not in rendered


@pytest.mark.asyncio
async def test_rebuild_reads_covered_originals_only_from_journal(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
) -> None:
    await seed_originals(repository, journals, range(1, 4))
    stored = envelope(
        version=2,
        through=3,
        generations=[
            CompressionGeneration(
                generation=2,
                from_sequence=1,
                through_sequence=3,
                messages=[{"role": "assistant", "content": "HEADROOM_ONLY"}],
                tokens_before=30,
                tokens_after=10,
                created_at="2026-08-06T00:00:00+00:00",
                ccr_expires_at="2026-08-06T12:00:00+00:00",
            )
        ],
    )
    assert await repository.compare_and_set_envelope("u", "s", 0, stored)

    candidate = await GenerationPlanner(repository, journals, max_segments=8).plan_rebuild(
        "u", "s", 3
    )

    assert candidate is not None
    assert candidate.rebuild is True
    assert candidate.expected_version == 2
    assert [event.content for event in candidate.originals] == [
        "original-1",
        "original-2",
        "original-3",
    ]


@pytest.mark.asyncio
async def test_incremental_waits_for_first_missing_sequence_then_uses_contiguous_prefix(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
) -> None:
    events = []
    for sequence in range(1, 4):
        content = f"original-{sequence}"
        reservation = await repository.reserve_event(
            "u", "s", f"e-{sequence}", sha256(content.encode()).hexdigest()
        )
        event = memory_event(
            sequence=reservation.sequence,
            event_id=f"e-{sequence}",
            content=content,
        )
        events.append(event)
        journals.append_event("u", "s", event)

    await repository.commit_event("u", "s", events[1])
    assert (
        await GenerationPlanner(repository, journals, max_segments=8).plan_incremental(
            "u", "s"
        )
        is None
    )

    await repository.commit_event("u", "s", events[0])
    await repository.commit_event("u", "s", events[2])
    planner = GenerationPlanner(repository, journals, max_segments=8)
    first = await planner.plan_incremental("u", "s")
    assert first is not None
    assert [event.sequence for event in first.originals] == [1, 2, 3]


@pytest.mark.asyncio
async def test_incremental_stops_before_a_later_sequence_gap(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
) -> None:
    events = []
    for sequence in range(1, 4):
        content = f"original-{sequence}"
        reservation = await repository.reserve_event(
            "u", "s", f"e-{sequence}", sha256(content.encode()).hexdigest()
        )
        event = memory_event(
            sequence=reservation.sequence,
            event_id=f"e-{sequence}",
            content=content,
        )
        events.append(event)
        journals.append_event("u", "s", event)

    await repository.commit_event("u", "s", events[0])
    await repository.commit_event("u", "s", events[2])
    planner = GenerationPlanner(repository, journals, max_segments=8)
    first = await planner.plan_incremental("u", "s")
    assert first is not None
    assert [event.sequence for event in first.originals] == [1]
    assert await repository.compare_and_set_envelope("u", "s", 0, envelope_from(first, "HR-1"))

    await repository.commit_event("u", "s", events[1])
    second = await planner.plan_incremental("u", "s")
    assert second is not None
    assert [event.sequence for event in second.originals] == [2, 3]


@pytest.mark.asyncio
async def test_rebuild_rejects_a_missing_journal_sequence(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
) -> None:
    journals.append_event("u", "s", memory_event(sequence=1, event_id="e-1"))
    journals.append_event("u", "s", memory_event(sequence=3, event_id="e-3"))

    with pytest.raises(OriginalSequenceGapError, match="missing sequence 2"):
        await GenerationPlanner(repository, journals, max_segments=8).plan_rebuild(
            "u", "s", 3
        )


@pytest.mark.asyncio
async def test_incremental_rejects_conflicting_duplicate_sequences(
    journals: JournalStore,
) -> None:
    store = OriginalStoreWithEvents(
        (
            memory_event(sequence=1, event_id="first", content="first"),
            memory_event(sequence=1, event_id="second", content="second"),
        )
    )

    with pytest.raises(ValueError, match="conflicting original events"):
        await GenerationPlanner(store, journals, max_segments=8).plan_incremental(
            "u", "s"
        )


@pytest.mark.asyncio
async def test_incremental_safely_folds_identical_duplicate_sequences(
    journals: JournalStore,
) -> None:
    event = memory_event(sequence=1, event_id="e-1", content="original")
    candidate = await GenerationPlanner(
        OriginalStoreWithEvents((event, event)), journals, max_segments=8
    ).plan_incremental("u", "s")

    assert candidate is not None
    assert candidate.originals == (event,)


def test_read_assembly_keeps_unexpired_opaque_generations_and_recent_originals() -> None:
    fresh = CompressionGeneration(
        generation=2,
        from_sequence=3,
        through_sequence=4,
        messages=[{"role": "tool", "content": "FRESH", "tool_call_id": "opaque"}],
        tokens_before=20,
        tokens_after=8,
        created_at="2026-08-06T10:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )
    expired = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=2,
        messages=[{"role": "assistant", "content": "EXPIRED"}],
        tokens_before=20,
        tokens_after=8,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T01:00:00+00:00",
    )
    assembled = GenerationAssembler(max_segments=8).build_read_messages(
        envelope(
            version=2,
            through=4,
            generations=[expired, fresh],
        ),
        (memory_event(sequence=4, event_id="e-4", content="recent overlap"),),
        datetime(2026, 8, 6, 11, tzinfo=timezone.utc),
    )

    assert assembled[0] == {
        "role": "tool",
        "content": "FRESH",
        "tool_call_id": "opaque",
    }
    assert assembled[1] == {"role": "user", "content": "recent overlap"}
    assert "EXPIRED" not in json.dumps(assembled)


def test_read_assembly_limits_opaque_generations_to_latest_segments() -> None:
    generations = tuple(
        CompressionGeneration(
            generation=index,
            from_sequence=index,
            through_sequence=index,
            messages=[{"role": "assistant", "content": f"segment-{index}"}],
            tokens_before=10,
            tokens_after=5,
            created_at="2026-08-06T00:00:00+00:00",
            ccr_expires_at="2026-08-07T00:00:00+00:00",
        )
        for index in range(1, 4)
    )

    assembled = GenerationAssembler(max_segments=2).build_read_messages(
        envelope(version=3, through=3, generations=generations),
        (),
        datetime(2026, 8, 6, tzinfo=timezone.utc),
    )

    assert [message["content"] for message in assembled] == [
        "segment-2",
        "segment-3",
    ]


def test_read_assembly_preserves_null_opaque_fields() -> None:
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=[{"role": "tool", "content": None, "tool_call_id": None}],
        tokens_before=10,
        tokens_after=5,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )

    assembled = GenerationAssembler(max_segments=8).build_read_messages(
        envelope(version=1, through=1, generations=[generation]),
        (),
        datetime(2026, 8, 6, 11, tzinfo=timezone.utc),
    )

    assert assembled[0] == {"role": "tool", "content": None, "tool_call_id": None}


@pytest.mark.parametrize(
    ("expires_at", "now"),
    [
        (
            "2026-08-06T12:00:00+00:00",
            datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
        ),
        (
            "2026-08-06T20:00:00+08:00",
            datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
        ),
    ],
)
def test_read_assembly_treats_exact_expiry_as_expired(
    expires_at: str, now: datetime
) -> None:
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=[{"role": "assistant", "content": "expired"}],
        tokens_before=10,
        tokens_after=5,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at=expires_at,
    )

    assembled = GenerationAssembler(max_segments=8).build_read_messages(
        envelope(version=1, through=1, generations=[generation]), (), now
    )

    assert assembled == ()


def test_read_assembly_rejects_naive_expiry_and_now() -> None:
    naive_expiry = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=[{"role": "assistant", "content": "opaque"}],
        tokens_before=10,
        tokens_after=5,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00",
    )
    assembler = GenerationAssembler(max_segments=8)

    with pytest.raises(ValueError, match="ccr_expires_at must be timezone-aware"):
        assembler.build_read_messages(
            envelope(version=1, through=1, generations=[naive_expiry]),
            (),
            datetime(2026, 8, 6, 11, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        assembler.build_read_messages(
            None,
            (),
            datetime(2026, 8, 6, 11),
        )

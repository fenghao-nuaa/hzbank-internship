from hashlib import sha256
import json

import pytest

from short_term_memory.models import JournalRole, MemoryEvent
from short_term_memory.storage.async_redis_memory_store import (
    AsyncRedisMemoryStore,
    EventConflictError,
)
from tests.factories import envelope, memory_event
from tests.storage.fake_redis import AsyncFakeRedis


@pytest.fixture
def redis() -> AsyncFakeRedis:
    return AsyncFakeRedis()


@pytest.fixture
def memory_store(redis: AsyncFakeRedis) -> AsyncRedisMemoryStore:
    return AsyncRedisMemoryStore(redis)


@pytest.mark.asyncio
async def test_reserve_retry_and_conflict(memory_store: AsyncRedisMemoryStore) -> None:
    first = await memory_store.reserve_event("u", "s", "e", "a" * 64)
    retry = await memory_store.reserve_event("u", "s", "e", "a" * 64)

    assert first.sequence == retry.sequence == 1
    assert first.state == "reserved"
    assert retry.state == "pending"
    with pytest.raises(EventConflictError):
        await memory_store.reserve_event("u", "s", "e", "b" * 64)


@pytest.mark.asyncio
async def test_reservation_tracks_event_id_in_pending_set_until_commit(
    redis: AsyncFakeRedis,
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(sequence=1, event_id="event")
    pending_key = "dream:session:u:s:pending-reservations"

    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)

    assert redis.sets[pending_key] == {"event"}
    assert redis.ttls[pending_key] == 43_200

    await memory_store.commit_event("u", "s", event)

    assert pending_key not in redis.sets
    assert pending_key not in redis.ttls


@pytest.mark.asyncio
async def test_commit_makes_event_visible_once(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(event_id="e")
    reservation = await memory_store.reserve_event("u", "s", "e", event.sha256)
    event = event.model_copy(update={"sequence": reservation.sequence})

    assert await memory_store.commit_event("u", "s", event) == "committed"
    assert await memory_store.commit_event("u", "s", event) == "duplicate"
    assert await memory_store.read_recent_originals("u", "s", 10) == (event,)


@pytest.mark.asyncio
async def test_read_originals_after_returns_only_later_original_events(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    first = memory_event(sequence=1, event_id="first", content="first")
    second = memory_event(sequence=2, event_id="second", content="second")
    for event in (first, second):
        await memory_store.reserve_event("u", "s", event.event_id, event.sha256)
        await memory_store.commit_event("u", "s", event)

    assert await memory_store.read_originals_after("u", "s", 1) == (second,)


@pytest.mark.asyncio
async def test_summary_cas_rejects_stale_worker(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    assert await memory_store.compare_and_set_envelope("u", "s", 0, envelope(version=1))
    assert not await memory_store.compare_and_set_envelope("u", "s", 0, envelope(version=2))
    assert (await memory_store.read_envelope("u", "s")).version == 1


@pytest.mark.asyncio
async def test_read_envelope_lazily_migrates_v1_without_semantic_categories(
    redis: AsyncFakeRedis,
    memory_store: AsyncRedisMemoryStore,
) -> None:
    redis.values["dream:session:u:s:summary"] = json.dumps(
        {
            "version": 3,
            "compressed_through_sequence": 7,
            "compression_generations": [],
            "current_goal": ["legacy"],
            "preferences": ["brief"],
            "confirmed_facts": ["fact"],
            "pending_items": ["pending"],
            "attachment_references": [],
            "updated_at": "2026-08-06T00:00:00+00:00",
        }
    )

    migrated = await memory_store.read_envelope("u", "s")

    assert migrated is not None
    assert migrated.schema_version == 2
    assert migrated.version == 3
    assert migrated.active_revision is None
    assert "current_goal" not in migrated.model_dump()


@pytest.mark.asyncio
async def test_reservation_commit_and_summary_refresh_consistent_ttls(
    redis: AsyncFakeRedis, memory_store: AsyncRedisMemoryStore
) -> None:
    event = memory_event(sequence=1, event_id="event")
    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)
    await memory_store.compare_and_set_envelope("u", "s", 0, envelope())
    await memory_store.commit_event("u", "s", event)

    prefix = "dream:session:u:s"
    expected = {
        f"{prefix}:sequence",
        f"{prefix}:messages",
        f"{prefix}:summary",
        f"{prefix}:event:event",
    }
    assert redis.ttls == {
        key: 43_200 for key in expected
    }


@pytest.mark.asyncio
async def test_compression_lease_is_exclusive_and_token_scoped(
    redis: AsyncFakeRedis,
    memory_store: AsyncRedisMemoryStore,
) -> None:
    assert await memory_store.acquire_compression_lease("u", "s", "one")
    assert not await memory_store.acquire_compression_lease("u", "s", "two")
    assert not await memory_store.release_compression_lease("u", "s", "two")
    assert await memory_store.release_compression_lease("u", "s", "one")
    assert await memory_store.acquire_compression_lease("u", "s", "two")
    redis.expire_now("dream:session:u:s:compression-lock")
    assert await memory_store.acquire_compression_lease("u", "s", "three")


@pytest.mark.asyncio
async def test_session_memory_extraction_lease_is_single_owner_and_expires_after_sixty_seconds(
    redis: AsyncFakeRedis,
    memory_store: AsyncRedisMemoryStore,
) -> None:
    started_at = "2026-08-14T08:00:00+00:00"
    assert await memory_store.acquire_session_memory_extraction(
        "u", "s", "owner", expected_version=2, started_at=started_at
    )
    assert not await memory_store.acquire_session_memory_extraction(
        "u", "s", "other", expected_version=2, started_at=started_at
    )
    state = await memory_store.read_session_memory_extraction("u", "s")
    assert state is not None
    assert state.token == "owner"
    assert state.expected_version == 2
    assert state.started_at == started_at
    key = "dream:session:u:s:session-memory-extraction"
    assert redis.ttls[key] == 60

    redis.expire_now(key)
    assert await memory_store.acquire_session_memory_extraction(
        "u", "s", "other", expected_version=2, started_at=started_at
    )


@pytest.mark.asyncio
async def test_session_memory_extraction_release_compares_owner_token(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    assert await memory_store.acquire_session_memory_extraction(
        "u", "s", "owner", expected_version=1,
        started_at="2026-08-14T08:00:00+00:00",
    )
    assert not await memory_store.release_session_memory_extraction("u", "s", "other")
    assert await memory_store.release_session_memory_extraction("u", "s", "owner")


@pytest.mark.asyncio
async def test_context_compaction_lease_is_session_scoped_and_token_owned(
    redis: AsyncFakeRedis,
    memory_store: AsyncRedisMemoryStore,
) -> None:
    assert await memory_store.acquire_context_compaction_lease("u", "s", "one")
    assert not await memory_store.acquire_context_compaction_lease("u", "s", "two")
    assert not await memory_store.release_context_compaction_lease("u", "s", "two")
    assert await memory_store.release_context_compaction_lease("u", "s", "one")
    assert redis.ttls.get("dream:session:u:s:context-compaction-lock") is None


@pytest.mark.asyncio
async def test_commit_rejects_different_digest_for_reserved_event(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(sequence=1, event_id="event", content="original")
    conflicting_event = memory_event(sequence=1, event_id="event", content="changed")
    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)

    with pytest.raises(EventConflictError, match="digest"):
        await memory_store.commit_event("u", "s", conflicting_event)

    assert await memory_store.read_recent_originals("u", "s", 10) == ()


@pytest.mark.asyncio
async def test_commit_rejects_wrong_sequence_for_reserved_event(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(sequence=1, event_id="event")
    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)

    with pytest.raises(ValueError, match="sequence"):
        await memory_store.commit_event(
            "u", "s", event.model_copy(update={"sequence": 2})
        )

    assert await memory_store.read_recent_originals("u", "s", 10) == ()


@pytest.mark.asyncio
async def test_duplicate_rechecks_reservation_digest_and_sequence(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(sequence=1, event_id="event")
    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)
    assert await memory_store.commit_event("u", "s", event) == "committed"

    with pytest.raises(EventConflictError, match="digest"):
        await memory_store.commit_event(
            "u", "s", memory_event(sequence=1, event_id="event", content="changed")
        )
    with pytest.raises(ValueError, match="sequence"):
        await memory_store.commit_event(
            "u", "s", event.model_copy(update={"sequence": 2})
        )


@pytest.mark.asyncio
async def test_restore_originals_preserves_sequences_and_advances_next_reservation(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    originals = tuple(
        memory_event(sequence=sequence, event_id=f"event-{sequence}")
        for sequence in range(91, 101)
    )

    assert await memory_store.restore_originals("u", "s", originals) is True
    assert await memory_store.read_recent_originals("u", "s", 10) == originals
    next_reservation = await memory_store.reserve_event("u", "s", "event-101", "a" * 64)

    assert next_reservation.sequence == 101


@pytest.mark.asyncio
async def test_restore_session_projection_sets_history_max_not_tail_max(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    tail = tuple(
        memory_event(sequence=sequence, event_id=f"event-{sequence}")
        for sequence in range(91, 101)
    )
    restored_envelope = envelope(version=7)

    assert await memory_store.restore_session_projection(
        "u",
        "s",
        latest_sequence=180,
        originals=tail,
        envelope=restored_envelope,
    )
    assert await memory_store.read_latest_sequence("u", "s") == 180
    assert await memory_store.read_recent_originals("u", "s", 10) == tail
    assert await memory_store.read_envelope("u", "s") == restored_envelope
    assert (
        await memory_store.reserve_event("u", "s", "event-181", "a" * 64)
    ).sequence == 181


@pytest.mark.asyncio
async def test_restore_session_projection_refuses_live_or_pending_state(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    await memory_store.reserve_event("u", "s", "live", "a" * 64)

    assert not await memory_store.restore_session_projection(
        "u",
        "s",
        latest_sequence=100,
        originals=(memory_event(sequence=100, event_id="history"),),
        envelope=None,
    )
    assert await memory_store.read_latest_sequence("u", "s") == 1


@pytest.mark.asyncio
async def test_restore_session_projection_validates_history_sequence(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    with pytest.raises(ValueError, match="latest_sequence"):
        await memory_store.restore_session_projection(
            "u",
            "s",
            latest_sequence=99,
            originals=(memory_event(sequence=100, event_id="history"),),
            envelope=None,
        )


@pytest.mark.asyncio
async def test_session_activation_lease_is_exclusive_and_token_scoped(
    redis: AsyncFakeRedis,
    memory_store: AsyncRedisMemoryStore,
) -> None:
    assert await memory_store.acquire_session_activation_lease("u", "s", "one")
    assert not await memory_store.acquire_session_activation_lease("u", "s", "two")
    assert not await memory_store.release_session_activation_lease("u", "s", "two")
    assert await memory_store.release_session_activation_lease("u", "s", "one")
    assert await memory_store.acquire_session_activation_lease("u", "s", "two")
    assert redis.ttls["dream:session:u:s:activation-lock"] == 60


@pytest.mark.asyncio
async def test_restore_refuses_any_live_sequence_counter_or_reservation(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    first = await memory_store.reserve_event("u", "s", "live", "a" * 64)
    originals = tuple(
        memory_event(sequence=sequence, event_id=f"event-{sequence}")
        for sequence in range(91, 101)
    )

    assert first.sequence == 1
    assert not await memory_store.restore_originals("u", "s", originals)
    assert (await memory_store.reserve_event("u", "s", "next", "b" * 64)).sequence == 2
    assert await memory_store.read_recent_originals("u", "s", 5) == ()


@pytest.mark.asyncio
async def test_restore_originals_never_overwrites_newer_redis_state(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    existing = memory_event(sequence=101, event_id="existing")
    assert await memory_store.restore_originals("u", "s", (existing,))

    assert not await memory_store.restore_originals(
        "u", "s", (memory_event(sequence=91, event_id="journal-event"),)
    )
    assert await memory_store.read_recent_originals("u", "s", 1) == (existing,)


@pytest.mark.asyncio
async def test_restore_originals_conflict_leaves_no_partial_state(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    existing = memory_event(sequence=1, event_id="same", content="redis")
    await memory_store.reserve_event("u", "s", existing.event_id, existing.sha256)
    await memory_store.commit_event("u", "s", existing)

    with pytest.raises(EventConflictError):
        await memory_store.restore_originals(
            "u",
            "s",
            (
                memory_event(sequence=1, event_id="same", content="journal"),
                memory_event(sequence=2, event_id="later"),
            ),
        )
    assert await memory_store.read_recent_originals("u", "s", 1) == (existing,)


@pytest.mark.asyncio
async def test_read_recent_originals_interprets_limit_as_turns(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    events = (
        memory_event(sequence=1, event_id="one", content="one"),
        memory_event(sequence=2, event_id="two", content="two").model_copy(
            update={"role": JournalRole.ASSISTANT}
        ),
        memory_event(sequence=3, event_id="three", content="three"),
    )
    for event in events:
        await memory_store.reserve_event("u", "s", event.event_id, event.sha256)
        await memory_store.commit_event("u", "s", event)

    assert await memory_store.read_recent_originals("u", "s", 1) == events[2:]


@pytest.mark.asyncio
async def test_recent_originals_are_sequence_ordered_even_if_committed_out_of_order(
    redis: AsyncFakeRedis,
    memory_store: AsyncRedisMemoryStore,
) -> None:
    later = memory_event(sequence=2, event_id="later", content="later")
    first = memory_event(sequence=1, event_id="first", content="first")
    redis.lists["dream:session:u:s:messages"] = [
        later.model_dump_json(), first.model_dump_json()
    ]

    assert await memory_store.read_recent_originals("u", "s", 2) == (first, later)


@pytest.mark.asyncio
async def test_key_components_are_validated_before_persistence(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await memory_store.reserve_event("../u", "s", "event", "a" * 64)
    with pytest.raises(ValueError, match="event_id"):
        await memory_store.reserve_event("u", "s", "event/id", "a" * 64)
    with pytest.raises(ValueError, match="digest"):
        await memory_store.reserve_event("u", "s", "event", "z" * 64)


def test_memory_event_fixture_digest_is_well_formed() -> None:
    event: MemoryEvent = memory_event()
    assert event.sha256 == sha256(event.content.encode()).hexdigest()

from datetime import datetime, timezone

import anyio
import pytest

from short_term_memory.models import (
    CompactBoundary,
    ContextRevision,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
    SessionMemoryRevision,
)
from short_term_memory.service.session_activation import SessionActivator
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.compaction_checkpoint import checkpoint_from_envelope
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.factories import memory_event
from tests.storage.fake_redis import AsyncFakeRedis


NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs = []

    async def enqueue(self, job):
        self.jobs.append(job)
        return "ready"


class FailingJournal:
    def read_latest_compaction_checkpoint(self, user_id, session_id):
        raise OSError("journal unavailable")

    def latest_original_sequence(self, user_id, session_id):
        raise AssertionError("must stop after the first Journal failure")


def continuity_envelope(*, version=7, through=170, with_l3=True):
    memory = SessionMemoryRevision(
        version=2,
        content="session memory",
        covered_through_sequence=through,
        token_count=500,
        updated_at=NOW.isoformat(),
    )
    revision = None
    if with_l3:
        boundary = CompactBoundary(
            boundary_id="boundary-1",
            trigger="auto",
            strategy="session_memory",
            covered_through_sequence=through,
            pre_compact_tokens=10_000,
            true_post_compact_tokens=1_000,
            created_at=NOW.isoformat(),
        )
        revision = ContextRevision(
            version=3,
            boundary=boundary,
            summary_message=SessionCompressionMessage(
                role="user", content="continuity summary"
            ),
            updated_at=NOW.isoformat(),
        )
    return MemorySummaryEnvelope(
        version=version,
        compressed_through_sequence=through,
        session_memory=memory,
        active_revision=revision,
        updated_at=NOW.isoformat(),
    )


def seed_history(journals: JournalStore, *, through: int) -> None:
    for sequence in range(1, through + 1):
        event = memory_event(
            sequence=sequence,
            event_id=f"event-{sequence}",
            content=f"message-{sequence}",
        )
        if sequence % 2 == 0:
            event = event.model_copy(update={"role": "assistant"})
        journals.append_event("u", "s", event)


@pytest.fixture
def activation_parts(tmp_path):
    redis = AsyncFakeRedis()
    store = AsyncRedisMemoryStore(redis)
    journals = JournalStore(VFSAdapter(tmp_path))
    queue = RecordingQueue()
    activator = SessionActivator(
        store=store,
        journals=journals,
        compression_queue=queue,
        history_turns=5,
        activation_timeout_seconds=1,
        clock=lambda: NOW,
    )
    return store, journals, queue, activator


@pytest.mark.asyncio
async def test_cold_activation_restores_checkpoint_tail_sequence_and_queues_rebuild(
    activation_parts,
) -> None:
    store, journals, queue, activator = activation_parts
    seed_history(journals, through=180)
    checkpoint = checkpoint_from_envelope("u", "s", continuity_envelope())
    journals.append_compaction_checkpoint("u", "s", checkpoint)

    result = await activator.activate("u", "s", history_turns=5)

    assert result.recovered
    assert result.latest_sequence == 180
    assert result.checkpoint_id == checkpoint.checkpoint_id
    assert await store.read_latest_sequence("u", "s") == 180
    restored = await store.read_envelope("u", "s")
    assert restored is not None and restored.active_revision is not None
    assert len(await store.read_recent_originals("u", "s", 5)) == 10
    assert len(queue.jobs) == 1
    assert queue.jobs[0].rebuild is True
    assert queue.jobs[0].requested_through_sequence == 180


@pytest.mark.asyncio
async def test_l4_only_checkpoint_materializes_recovery_revision(
    activation_parts,
) -> None:
    store, journals, _, activator = activation_parts
    seed_history(journals, through=180)
    journals.append_compaction_checkpoint(
        "u", "s", checkpoint_from_envelope("u", "s", continuity_envelope(with_l3=False))
    )

    await activator.activate("u", "s")

    restored = await store.read_envelope("u", "s")
    assert restored is not None
    assert restored.active_revision is not None
    assert restored.active_revision.boundary.strategy == "session_memory"


@pytest.mark.asyncio
async def test_warm_activation_is_idempotent_and_heals_missing_checkpoint(
    activation_parts,
) -> None:
    store, journals, queue, activator = activation_parts
    live = continuity_envelope(version=9)
    assert await store.restore_session_projection(
        "u", "s", latest_sequence=180, originals=(), envelope=live
    )

    result = await activator.activate("u", "s")

    assert not result.recovered
    assert await store.read_envelope("u", "s") == live
    healed = journals.read_latest_compaction_checkpoint("u", "s")
    assert healed is not None and healed.envelope_version == 9
    assert queue.jobs == []


@pytest.mark.asyncio
async def test_legacy_session_restores_recent_turns_without_checkpoint(
    activation_parts,
) -> None:
    store, journals, queue, activator = activation_parts
    seed_history(journals, through=40)

    result = await activator.activate("u", "s")

    assert result.recovered
    assert result.checkpoint_id is None
    assert await store.read_latest_sequence("u", "s") == 40
    assert await store.read_envelope("u", "s") is None
    assert len(await store.read_recent_originals("u", "s", 5)) == 10
    assert queue.jobs[0].rebuild is True


@pytest.mark.asyncio
async def test_checkpoint_coverage_beyond_journal_is_ignored(
    activation_parts,
) -> None:
    store, journals, _, activator = activation_parts
    seed_history(journals, through=180)
    corrupt = continuity_envelope(through=200)
    journals.append_compaction_checkpoint(
        "u", "s", checkpoint_from_envelope("u", "s", corrupt)
    )

    result = await activator.activate("u", "s")

    assert result.recovered
    assert result.checkpoint_id is None
    assert await store.read_envelope("u", "s") is None


@pytest.mark.asyncio
async def test_journal_failure_aborts_without_redis_projection() -> None:
    store = AsyncRedisMemoryStore(AsyncFakeRedis())
    activator = SessionActivator(
        store=store,
        journals=FailingJournal(),
        compression_queue=RecordingQueue(),
        history_turns=5,
        activation_timeout_seconds=1,
        clock=lambda: NOW,
    )

    with pytest.raises(OSError, match="journal unavailable"):
        await activator.activate("u", "s")

    assert await store.read_latest_sequence("u", "s") == 0


@pytest.mark.asyncio
async def test_contending_activation_waits_for_single_projection_writer(
    activation_parts,
) -> None:
    store, _, queue, activator = activation_parts
    assert await store.acquire_session_activation_lease("u", "s", "owner")

    async def finish_owner() -> None:
        await anyio.sleep(0.05)
        await store.restore_session_projection(
            "u",
            "s",
            latest_sequence=40,
            originals=(memory_event(sequence=40, event_id="tail"),),
            envelope=None,
        )
        await store.release_session_activation_lease("u", "s", "owner")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(finish_owner)
        result = await activator.activate("u", "s")

    assert not result.recovered
    assert result.latest_sequence == 40
    assert queue.jobs == []

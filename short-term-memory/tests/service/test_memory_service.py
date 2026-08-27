from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from short_term_memory.compression.generations import GenerationAssembler
from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.models import CompressionGeneration, EventReservation
from short_term_memory.service.memory_service import (
    MemoryReadUnavailableError,
    MemoryService,
    MemoryTranscriptScopeError,
    RetryableWriteError,
)
from short_term_memory.service.schemas import (
    MemoryReadRequest,
    MemoryTranscriptGrepRequest,
    MemoryTranscriptReadRequest,
)
from short_term_memory.storage.async_redis_memory_store import (
    AsyncRedisMemoryStore,
    EventConflictError,
)
from short_term_memory.storage.journal_store import JournalAppendResult, JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.storage.fake_redis import AsyncFakeRedis
from tests.factories import envelope, memory_event, read_request, write_request


class RecordingStore:
    def __init__(self, calls):
        self.calls = calls
        self.events = []
        self.envelope = None
        self.reservations = {}
        self.fail_next_commit = False
        self.commit_error = None
        self.fail_envelope_read = False
        self.fail_original_read = False

    async def reserve_event(self, user_id, session_id, event_id, digest):
        self.calls.append("reserve")
        if event_id in self.reservations:
            return self.reservations[event_id]
        reservation = EventReservation(sequence=len(self.reservations) + 1, state="reserved")
        self.reservations[event_id] = reservation
        return reservation

    async def commit_event(self, user_id, session_id, event):
        self.calls.append("redis_commit")
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise OSError("redis unavailable")
        if self.commit_error is not None:
            raise self.commit_error
        if event not in self.events:
            self.events.append(event)
            self.reservations[event.event_id] = EventReservation(
                sequence=event.sequence, state="committed"
            )
            return "committed"
        return "duplicate"

    async def read_envelope(self, user_id, session_id):
        if self.fail_envelope_read:
            if isinstance(self.fail_envelope_read, Exception):
                raise self.fail_envelope_read
            raise OSError("redis unavailable")
        return self.envelope

    async def read_recent_originals(self, user_id, session_id, history_turns):
        if self.fail_original_read:
            raise OSError("redis unavailable")
        events = tuple(self.events[-(history_turns * 2):])
        return events[1:] if events and events[0].role.value == "assistant" else events

    async def read_originals_after(self, user_id, session_id, sequence):
        return tuple(event for event in self.events if event.sequence > sequence)

    def seed_envelope(self, value):
        self.envelope = value

    async def restore_originals(self, user_id, session_id, originals):
        if self.events:
            return False
        self.events = list(originals)
        return True


class RecordingJournals:
    def __init__(self, calls):
        self.calls = calls
        self.events = []
        self.read_error = None

    def append_event(self, user_id, session_id, event):
        self.calls.append("journal_fsync")
        if not any(existing.event_id == event.event_id for existing in self.events):
            self.events.append(event)
            return JournalAppendResult(appended=True, path=Path("journal"))
        return JournalAppendResult(appended=False, path=Path("journal"))

    def append_count(self, event_id):
        return sum(event.event_id == event_id for event in self.events)

    def find_event(self, user_id, session_id, event_id):
        return next((event for event in self.events if event.event_id == event_id), None)

    def read_recent_originals(self, user_id, session_id, history_turns):
        return tuple(self.events[-(history_turns * 2):])

    def read_original_range(self, user_id, session_id, from_sequence, through_sequence):
        self.calls.append("journal_transcript_read")
        if self.read_error is not None:
            raise self.read_error
        return tuple(
            event
            for event in self.events
            if from_sequence <= event.sequence <= through_sequence
        )


class RecordingQueue:
    def __init__(self, calls):
        self.calls = calls
        self.jobs = []
        self.error = None

    async def enqueue(self, job):
        if self.error is not None:
            raise self.error
        self.calls.append("enqueue")
        self.jobs.append(job)
        return "ready"


class RecordingRebuildWaiter:
    def __init__(self, envelope=None, error=None):
        self.envelope = envelope
        self.error = error
        self.jobs = []

    async def wait_for(self, job, timeout_seconds):
        self.jobs.append((job, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.envelope


class RecordingPolicy(HeadroomPolicy):
    def __init__(self, calls):
        super().__init__(context_window_tokens=1, trigger_ratio=0.65, max_messages=1, max_session_seconds=1)
        self.calls = calls

    def should_compress(self, **kwargs):
        self.calls.append("policy")
        return True


@pytest.fixture
def service():
    calls = []
    store = RecordingStore(calls)
    journals = RecordingJournals(calls)
    return MemoryService(
        store=store,
        journals=journals,
        assembler=GenerationAssembler(max_segments=8),
        compression_queue=RecordingQueue(calls),
        policy=RecordingPolicy(calls),
        scope_factory=OptimizationScopeFactory("secret"),
        settings=ShortTermMemorySettings(),
        headroom_proxy_url="http://headroom:8787/v1",
        clock=lambda: datetime(2026, 8, 6, tzinfo=timezone.utc),
        token_estimator=lambda events: len(events),
    )


@pytest.mark.asyncio
async def test_write_reserves_journals_commits_then_queues(service):
    response = await service.write(write_request("event-1", "original"), "req-1")

    # Single message falls inside the retained recent-N-turns window, so no
    # compression or re-compression is enqueued.  The write path is still intact.
    assert "reserve" in service.store.calls
    assert "journal_fsync" in service.store.calls
    assert "redis_commit" in service.store.calls
    assert "enqueue" not in service.store.calls
    assert response.accepted is True
    assert response.sequence_from == response.sequence_through == 1
    assert service.journals.events[0].content == "original"


@pytest.mark.asyncio
async def test_generation_pressure_queues_explicit_oldest_eviction(service):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    existing = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=({"role": "system", "content": "compressed segment"},),
        tokens_before=100,
        tokens_after=80,
        created_at=now.isoformat(),
        ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    service.store.seed_envelope(envelope(through=1, generations=[existing]))

    await service.write(write_request("event-evict", "recent original"), "req-evict")

    assert len(service.compression_queue.jobs) == 1
    job = service.compression_queue.jobs[0]
    assert job.evict_oldest_generation is True
    assert "recompress" not in job.model_dump(mode="json")


@pytest.mark.asyncio
async def test_assistant_commit_schedules_l4_only_after_durable_write(service):
    from short_term_memory.service.schemas import MemoryWriteRequest

    l4_queue = RecordingQueue(service.store.calls)
    service.session_memory_queue = l4_queue
    service.token_estimator = lambda messages: 10_000
    request = MemoryWriteRequest.model_validate(
        {
            "user_id": "u",
            "session_id": "s",
            "events": [
                {
                    "event_id": "assistant-1",
                    "role": "assistant",
                    "content_type": "conversation",
                    "content": "completed response",
                    "metadata": {},
                }
            ],
        }
    )

    await service.write(request, "req-l4")

    assert len(l4_queue.jobs) == 1
    job = l4_queue.jobs[0]
    assert job.expected_version == 0
    assert job.requested_through_sequence == 1
    assert service.store.calls.index("redis_commit") < service.store.calls.index("enqueue")


@pytest.mark.asyncio
async def test_grep_transcript_validates_scope_and_reads_bound_session(service):
    service.journals.events.append(
        memory_event(sequence=87, event_id="ttl", content="TTL is 43200")
    )
    scope = service.scope_factory.for_session("u", "s").session_scope
    response = await service.grep_transcript(
        MemoryTranscriptGrepRequest(
            user_id="u",
            session_id="s",
            path="journal://current-session",
            pattern="TTL",
            output_mode="content",
        ),
        "req-grep",
        session_scope=scope,
    )

    assert response.request_id == "req-grep"
    assert response.matches[0].sequence == 87
    assert "journal_transcript_read" in service.journals.calls


@pytest.mark.asyncio
async def test_transcript_scope_mismatch_is_rejected_before_journal_access(service):
    request = MemoryTranscriptReadRequest(
        user_id="u",
        session_id="s",
        file_path="journal://current-session",
    )

    with pytest.raises(MemoryTranscriptScopeError):
        await service.read_transcript(
            request, "req-read", session_scope="wrong-private-scope"
        )

    assert "journal_transcript_read" not in service.journals.calls


@pytest.mark.asyncio
async def test_read_transcript_propagates_journal_failure(service):
    service.journals.read_error = OSError("/private/journal/path")
    scope = service.scope_factory.for_session("u", "s").session_scope

    with pytest.raises(OSError):
        await service.read_transcript(
            MemoryTranscriptReadRequest(
                user_id="u",
                session_id="s",
                file_path="journal://current-session",
            ),
            "req-read",
            session_scope=scope,
        )


@pytest.mark.asyncio
async def test_retry_after_commit_failure_repairs_without_duplicate_journal(service):
    service.store.fail_next_commit = True

    with pytest.raises(RetryableWriteError):
        await service.write(write_request("event-1", "original"), "req-1")

    response = await service.write(write_request("event-1", "original"), "req-2")

    assert response.accepted is True
    assert service.journals.append_count("event-1") == 1


@pytest.mark.asyncio
async def test_read_returns_proxy_scope_without_calling_deepseek(service):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=({"role": "system", "content": "HR-MARKER"},),
        tokens_before=2,
        tokens_after=1,
        created_at=now.isoformat(),
        ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    service.store.seed_envelope(envelope(through=1, generations=[generation]))
    service.store.events.append(memory_event())

    response = await service.read(read_request(), "req-2")

    assert any("HR-MARKER" in str(message.content) for message in response.messages)
    assert response.headroom.proxy_url == "http://headroom:8787/v1"
    assert set(response.headroom.scope_headers) == {
        "x-headroom-user-id", "x-headroom-session-id", "x-headroom-project-id"
    }
    assert not hasattr(service, "deepseek_client")


@pytest.mark.asyncio
async def test_read_recovers_recent_originals_from_journal_when_redis_is_empty(service):
    recovered = memory_event(sequence=1, event_id="event-1", content="journal-original")
    service.journals.events.append(recovered)

    response = await service.read(read_request(), "req-2")

    assert response.memory.source == "journal_rebuild"
    assert response.memory.latest_sequence == 1
    assert response.messages[-1].content == "journal-original"
    assert service.store.events == [recovered]


@pytest.mark.asyncio
async def test_read_history_mode_returns_compressed_view_not_journal_originals(service):
    """history=true must return the compressed summary only, never restore journal
    originals (so opening a historical session cannot fill the context)."""
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=({"role": "system", "content": "HR-COMPRESSED-SEGMENT"},),
        tokens_before=100,
        tokens_after=50,
        created_at=now.isoformat(),
        ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    service.store.seed_envelope(envelope(through=1, generations=[generation]))
    # Journal has originals that must NOT be restored in history mode.
    service.journals.events.append(
        memory_event(sequence=1, event_id="event-1", content="journal-original-not-for-history")
    )

    request = MemoryReadRequest(
        user_id="u",
        session_id="s",
        history_turns=10,
        include_effective_config=True,
        history=True,
    )
    response = await service.read(request, "req-2")

    # Compressed segment present; journal original absent.
    assert any("HR-COMPRESSED-SEGMENT" in str(m.content) for m in response.messages)
    assert not any("journal-original-not-for-history" in str(m.content) for m in response.messages)
    assert response.memory.source != "journal_rebuild"


@pytest.mark.asyncio
async def test_read_omits_effective_config_when_not_requested(service):
    request = read_request().model_copy(update={"include_effective_config": False})

    response = await service.read(request, "req-2")

    assert response.effective_config is None


@pytest.mark.asyncio
async def test_retry_commits_the_canonical_journal_event_despite_a_new_clock(
    tmp_path,
):
    store = AsyncRedisMemoryStore(AsyncFakeRedis())
    journals = JournalStore(VFSAdapter(tmp_path))
    service_at_t1 = MemoryService(
        store=store, journals=journals, assembler=GenerationAssembler(max_segments=8),
        compression_queue=RecordingQueue([]), policy=RecordingPolicy([]),
        scope_factory=OptimizationScopeFactory("secret"), settings=ShortTermMemorySettings(),
        headroom_proxy_url="http://headroom:8787/v1", token_estimator=lambda events: len(events),
        clock=lambda: datetime(2026, 8, 6, tzinfo=timezone.utc),
    )
    original_commit = store.commit_event
    failed = True

    async def fail_once(user_id, session_id, event):
        nonlocal failed
        if failed:
            failed = False
            raise OSError("redis down")
        return await original_commit(user_id, session_id, event)

    store.commit_event = fail_once
    with pytest.raises(RetryableWriteError):
        await service_at_t1.write(write_request(), "first")

    service_at_t2 = MemoryService(
        store=store, journals=journals, assembler=GenerationAssembler(max_segments=8),
        compression_queue=RecordingQueue([]), policy=RecordingPolicy([]),
        scope_factory=OptimizationScopeFactory("secret"), settings=ShortTermMemorySettings(),
        headroom_proxy_url="http://headroom:8787/v1", token_estimator=lambda events: len(events),
        clock=lambda: datetime(2026, 8, 7, tzinfo=timezone.utc),
    )
    await service_at_t2.write(write_request(), "retry")

    canonical = journals.find_event("u", "s", "event-1")
    assert canonical is not None
    assert (await store.read_recent_originals("u", "s", 1)) == (canonical,)
    assert canonical.created_at == "2026-08-06T00:00:00+00:00"


@pytest.mark.asyncio
async def test_batch_commit_failure_reports_prior_committed_ids_in_input_order(service):
    second = write_request("event-2", "second").events[0]
    request = write_request().model_copy(update={"events": [write_request().events[0], second]})
    service.store.commit_error = OSError("redis unavailable")
    original_commit = service.store.commit_event
    calls = 0

    async def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("redis unavailable")
        return await original_commit(*args)

    service.store.commit_error = None
    service.store.commit_event = fail_second
    with pytest.raises(RetryableWriteError) as raised:
        await service.write(request, "req-1")

    assert raised.value.committed_event_ids == ("event-1",)
    assert [event.event_id for event in service.store.events] == ["event-1"]


@pytest.mark.asyncio
async def test_write_preserves_commit_digest_conflicts(service):
    service.store.commit_error = EventConflictError("digest conflict")

    with pytest.raises(EventConflictError, match="digest conflict"):
        await service.write(write_request(), "req-1")


@pytest.mark.asyncio
async def test_duplicate_only_write_skips_policy_and_enqueue(service):
    await service.write(write_request(), "first")
    service.store.calls.clear()

    response = await service.write(write_request(), "retry")

    assert response.duplicate_event_ids == ["event-1"]
    assert response.compression_queued is False
    assert service.store.calls == ["reserve"]


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_state", ["ready", "pending", "idempotent", "coalesced"])
async def test_all_durable_queue_states_are_accepted(service, queue_state):
    async def enqueue(job):
        service.compression_queue.jobs.append(job)
        return queue_state

    service.compression_queue.enqueue = enqueue
    response = await service.write(write_request(), "req-1")

    assert response.compression_queued is True


@pytest.mark.asyncio
async def test_envelope_missing_with_redis_originals_enqueues_rebuild(service):
    service.store.events.append(memory_event(sequence=10))

    response = await service.read(read_request(), "req-1")

    assert response.memory.source == "redis"
    assert service.compression_queue.jobs[-1].rebuild is True
    assert service.compression_queue.jobs[-1].requested_through_sequence == 10


@pytest.mark.asyncio
async def test_expired_generation_degrades_to_originals_but_keeps_redis_source(service):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    expired = CompressionGeneration(
        generation=1, from_sequence=1, through_sequence=1,
        messages=({"role": "system", "content": "EXPIRED"},), tokens_before=2,
        tokens_after=1, created_at=(now - timedelta(hours=2)).isoformat(),
        ccr_expires_at=(now - timedelta(hours=1)).isoformat(),
    )
    service.store.seed_envelope(envelope(through=1, generations=[expired]))
    service.store.events.append(memory_event())

    fresh = CompressionGeneration(
        generation=2, from_sequence=1, through_sequence=1,
        messages=({"role": "system", "content": "FRESH"},), tokens_before=2,
        tokens_after=1, created_at=now.isoformat(),
        ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    service.rebuild_waiter = RecordingRebuildWaiter(
        envelope(version=2, through=1, generations=[fresh])
    )

    response = await service.read(read_request(), "req-1")

    assert response.memory.source == "journal_rebuild"
    assert response.memory.compression_segments == 1
    assert all("EXPIRED" not in str(message.content) for message in response.messages)
    assert service.compression_queue.jobs[-1].rebuild is True
    assert service.rebuild_waiter.jobs[-1][0].rebuild is True
    assert response.timing_ms.recovery > 0


@pytest.mark.asyncio
async def test_expired_generation_times_out_without_exposing_expired_opaque(service):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    expired = CompressionGeneration(
        generation=1, from_sequence=1, through_sequence=1,
        messages=({"role": "system", "content": "EXPIRED"},), tokens_before=2,
        tokens_after=1, created_at=(now - timedelta(hours=2)).isoformat(),
        ccr_expires_at=(now - timedelta(hours=1)).isoformat(),
    )
    service.store.seed_envelope(envelope(through=1, generations=[expired]))
    service.store.events.append(memory_event())
    service.rebuild_waiter = RecordingRebuildWaiter(error=TimeoutError("timed out"))

    with pytest.raises(MemoryReadUnavailableError, match="cold rebuild"):
        await service.read(read_request(), "req-1")


@pytest.mark.asyncio
async def test_expired_generation_rejects_waiter_result_without_new_coverage(service):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    expired = CompressionGeneration(
        generation=1, from_sequence=1, through_sequence=1,
        messages=({"role": "system", "content": "EXPIRED"},), tokens_before=2,
        tokens_after=1, created_at=(now - timedelta(hours=2)).isoformat(),
        ccr_expires_at=(now - timedelta(hours=1)).isoformat(),
    )
    service.store.seed_envelope(envelope(version=1, through=1, generations=[expired]))
    service.store.events.append(memory_event())
    fresh_but_stale_version = CompressionGeneration(
        generation=2, from_sequence=1, through_sequence=1,
        messages=({"role": "system", "content": "FRESH"},), tokens_before=2,
        tokens_after=1, created_at=now.isoformat(), ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    service.rebuild_waiter = RecordingRebuildWaiter(
        envelope(version=1, through=1, generations=[fresh_but_stale_version])
    )

    with pytest.raises(MemoryReadUnavailableError, match="fresh context"):
        await service.read(read_request(), "req-1")


@pytest.mark.asyncio
async def test_redis_read_failure_recovers_from_journal_without_aborting(service):
    recovered = memory_event(sequence=1, event_id="journal", content="journal")
    service.journals.events.append(recovered)
    service.store.fail_envelope_read = True
    service.store.fail_original_read = True

    response = await service.read(read_request(), "req-1")

    assert response.memory.source == "journal_rebuild"
    assert response.messages[-1].content == "journal"


@pytest.mark.asyncio
async def test_journal_read_survives_redis_restore_and_queue_infrastructure_failures(service):
    recovered = memory_event(sequence=1, event_id="journal", content="journal")
    service.journals.events.append(recovered)
    service.store.fail_envelope_read = True
    service.store.fail_original_read = True

    async def unavailable_restore(*args):
        raise OSError("redis restore unavailable")

    service.store.restore_originals = unavailable_restore
    service.compression_queue.error = OSError("queue unavailable")

    response = await service.read(read_request(), "req-1")

    assert response.memory.source == "journal_rebuild"
    assert response.messages[-1].content == "journal"


@pytest.mark.asyncio
async def test_redis_read_failure_with_empty_journal_is_explicit(service):
    service.store.fail_envelope_read = True
    service.store.fail_original_read = True

    with pytest.raises(MemoryReadUnavailableError):
        await service.read(read_request(), "req-1")


@pytest.mark.asyncio
async def test_read_propagates_non_infrastructure_redis_corruption(service):
    service.store.fail_envelope_read = ValueError("corrupt envelope")

    with pytest.raises(ValueError, match="corrupt envelope"):
        await service.read(read_request(), "req-1")


@pytest.mark.asyncio
async def test_real_store_and_journal_recovery_preserve_tail_sequences(tmp_path):
    store = AsyncRedisMemoryStore(AsyncFakeRedis())
    journals = JournalStore(VFSAdapter(tmp_path))
    originals = tuple(
        memory_event(sequence=sequence, event_id=f"event-{sequence}")
        for sequence in range(91, 101)
    )
    for event in originals:
        journals.append_event("u", "s", event)
    service = MemoryService(
        store=store, journals=journals, assembler=GenerationAssembler(max_segments=8),
        compression_queue=RecordingQueue([]), policy=RecordingPolicy([]),
        scope_factory=OptimizationScopeFactory("secret"), settings=ShortTermMemorySettings(),
        headroom_proxy_url="http://headroom:8787/v1", token_estimator=lambda events: len(events),
        clock=lambda: datetime(2026, 8, 6, tzinfo=timezone.utc),
    )

    response = await service.read(read_request().model_copy(update={"history_turns": 5}), "req-1")
    next_reservation = await store.reserve_event("u", "s", "event-101", "a" * 64)

    assert response.memory.source == "journal_rebuild"
    assert [event.sequence for event in await store.read_recent_originals("u", "s", 5)] == list(range(91, 101))
    assert next_reservation.sequence == 101


@pytest.mark.asyncio
async def test_empty_redis_and_empty_journal_is_an_empty_redis_source(service):
    response = await service.read(read_request(), "req-1")

    assert response.memory.source == "redis"
    assert response.memory.latest_sequence == 0
    assert response.messages == []


@pytest.mark.asyncio
async def test_coalesced_cold_rebuild_refreshes_latest_sequence_from_stronger_envelope(service):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    expired = CompressionGeneration(
        generation=1, from_sequence=1, through_sequence=5,
        messages=({"role": "system", "content": "EXPIRED"},),
        tokens_before=10, tokens_after=5,
        created_at=(now - timedelta(hours=2)).isoformat(),
        ccr_expires_at=(now - timedelta(hours=1)).isoformat(),
    )
    fresh = CompressionGeneration(
        generation=2, from_sequence=1, through_sequence=10,
        messages=({"role": "system", "content": "FRESH"},),
        tokens_before=20, tokens_after=5,
        created_at=now.isoformat(),
        ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    old = envelope(version=1, through=5, generations=[expired])
    stronger = envelope(version=2, through=10, generations=[fresh])
    service.store.envelope = old
    service.store.originals = tuple(
        memory_event(sequence=sequence, event_id=f"event-{sequence}")
        for sequence in range(1, 6)
    )
    service.rebuild_waiter = RecordingRebuildWaiter(stronger)

    response = await service.read(read_request(), "req-1")

    assert response.memory.compressed_through_sequence == 10
    assert response.memory.latest_sequence == 10

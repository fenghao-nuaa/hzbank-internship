import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from short_term_memory.compression.async_headroom_client import AsyncHeadroomClient
from short_term_memory.compression.generations import GenerationAssembler, GenerationPlanner
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.jobs.compression_worker import (
    CompressionWorkerResult,
    CompressionWorker,
    InProcessRebuildWaiter,
)
from short_term_memory.jobs.redis_compression_queue import CompressionJob, RedisCompressionQueue
from short_term_memory.models import (
    AutoCompactTrackingState,
    CompactBoundary,
    CompressionGeneration,
    ContextRevision,
    HeadroomCompressionResult,
    HeadroomCompressionStatus,
    HeadroomFailureReason,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
    SessionMemoryRevision,
)
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.factories import envelope, memory_event
from tests.jobs.test_redis_compression_queue import QueueRedis
from tests.storage.fake_redis import AsyncFakeRedis


class MarkerTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "messages": [{"role": "system", "content": "marker"}],
                "tokens_before": 100,
                "tokens_after": 25,
                "tokens_saved": 75,
                "compression_ratio": 4.0,
                "transforms_applied": ["test"],
            },
            request=request,
        )


class FailingHeadroom:
    async def compress(self, *args, **kwargs):
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.FAILED,
            messages=(),
            fallback_used=False,
            failure_reason=HeadroomFailureReason.SERVICE_UNAVAILABLE,
        )


class FailOnceHeadroom:
    def __init__(self):
        self.calls = 0

    async def compress(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return await FailingHeadroom().compress(*args, **kwargs)
        return await DelayedSuccessfulHeadroom().compress(*args, **kwargs)


class BlockingHeadroom:
    def __init__(self):
        self.started = asyncio.Event()

    async def compress(self, *args, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


class DelayedSuccessfulHeadroom:
    async def compress(self, *args, **kwargs):
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.SUCCESS,
            messages=({"role": "system", "content": "marker"},),
            fallback_used=False,
            tokens_before=100,
            tokens_after=25,
        )


class RecordingCompletionPublisher:
    def __init__(self, queue=None):
        self.queue = queue
        self.calls = []

    async def publish(self, job, completed_envelope):
        if self.queue is not None:
            assert await self.queue.ack(
                type("Lease", (), {"job": job, "token": "already-acked"})()
            ) is False
        self.calls.append((job, completed_envelope))


class FailingCompletionPublisher:
    async def publish(self, job, completed_envelope):
        raise ConnectionError("completion transport unavailable")


async def seed(store, journals, count=10):
    for sequence in range(1, count + 1):
        content = f"ORIGINAL-{sequence}"
        reservation = await store.reserve_event(
            "u", "s", f"event-{sequence}", sha256(content.encode()).hexdigest()
        )
        event = memory_event(sequence=reservation.sequence, event_id=f"event-{sequence}", content=content)
        journals.append_event("u", "s", event)
        await store.commit_event("u", "s", event)


@pytest_asyncio.fixture
async def worker(tmp_path):
    store = AsyncRedisMemoryStore(AsyncFakeRedis())
    journals = JournalStore(VFSAdapter(tmp_path))
    await seed(store, journals)
    transport = MarkerTransport()
    headroom = AsyncHeadroomClient("http://headroom:8787", timeout_seconds=5, transport=transport)
    worker = CompressionWorker(
        queue=RedisCompressionQueue(QueueRedis()),
        store=store,
        planner=GenerationPlanner(store, journals, max_segments=8),
        headroom=headroom,
        compression_model="deepseek-v4-flash",
        scope_factory=OptimizationScopeFactory("secret"),
        ccr_ttl_seconds=43_200,
        ccr_refresh_seconds=3_600,
        max_segments=8,
    )
    yield worker, store, transport
    await headroom.aclose()


def compression_job(*, through_sequence=10, expected_version=0):
    return CompressionJob(
        job_id=f"job-{through_sequence}-{expected_version}", user_id="u", session_id="s",
        expected_version=expected_version, requested_through_sequence=through_sequence, attempt=0,
    )


def generation(number: int, start: int, through: int, content: str) -> CompressionGeneration:
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    return CompressionGeneration(
        generation=number,
        from_sequence=start,
        through_sequence=through,
        messages=(SessionCompressionMessage(role="system", content=content),),
        tokens_before=100,
        tokens_after=25,
        created_at=now.isoformat(),
        ccr_expires_at=(now + timedelta(days=1)).isoformat(),
    )


def continuity_envelope() -> MemorySummaryEnvelope:
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    boundary = CompactBoundary(
        boundary_id="boundary-10",
        trigger="auto",
        strategy="traditional",
        covered_through_sequence=10,
        pre_compact_tokens=1000,
        true_post_compact_tokens=100,
        created_at=now.isoformat(),
    )
    return MemorySummaryEnvelope(
        version=1,
        compressed_through_sequence=5,
        compression_generations=(generation(1, 1, 5, "old marker"),),
        session_memory=SessionMemoryRevision(
            version=1,
            content="L4 CONTINUITY",
            covered_through_sequence=5,
            token_count=20,
            updated_at=now.isoformat(),
        ),
        active_revision=ContextRevision(
            version=1,
            boundary=boundary,
            summary_message=SessionCompressionMessage(
                role="user", content="L3 CONTINUITY"
            ),
            covered_generation_ids=(1,),
            updated_at=now.isoformat(),
        ),
        auto_compact_tracking=AutoCompactTrackingState(
            compacted=True, turn_counter=0, turn_id="turn-1"
        ),
        updated_at=now.isoformat(),
    )


@pytest.mark.asyncio
async def test_worker_stores_generation_only_after_headroom_success(worker):
    worker, store, transport = worker
    await worker.queue.enqueue(compression_job())

    result = await worker.run_once()
    persisted = await store.read_envelope("u", "s")

    assert result.state == "acked"
    assert persisted is not None
    assert persisted.compressed_through_sequence == 10
    assert persisted.compression_generations[0].messages[0].content == "marker"
    assert b"ORIGINAL-1" in transport.requests[0].content
    assert b"marker" not in transport.requests[0].content


@pytest.mark.asyncio
async def test_generation_worker_preserves_continuity_state_and_hides_late_coverage(
    worker,
):
    worker, store, transport = worker
    existing = continuity_envelope()
    assert await store.compare_and_set_envelope("u", "s", 0, existing)
    await worker.queue.enqueue(compression_job(expected_version=1))

    result = await worker.run_once()
    saved = await store.read_envelope("u", "s")

    assert result.state == "acked"
    assert saved is not None
    assert saved.session_memory == existing.session_memory
    assert saved.active_revision == existing.active_revision
    assert saved.auto_compact_tracking == existing.auto_compact_tracking
    assert [item.generation for item in saved.compression_generations] == [1, 2]
    request_body = transport.requests[0].content
    assert b"ORIGINAL-6" in request_body and b"ORIGINAL-10" in request_body
    assert b"ORIGINAL-5" not in request_body
    assert b"L3 CONTINUITY" not in request_body
    assert b"L4 CONTINUITY" not in request_body
    active = GenerationAssembler(max_segments=8).build_read_messages(
        saved, (), datetime.now(timezone.utc)
    )
    assert "marker" not in [item["content"] for item in active]
    assert "L3 CONTINUITY" in [item["content"] for item in active]


@pytest.mark.asyncio
async def test_generation_eviction_drops_only_oldest_and_keeps_ccr_catalog(worker):
    worker, store, _ = worker
    existing = MemorySummaryEnvelope(
        version=1,
        compressed_through_sequence=10,
        compression_generations=(
            generation(1, 1, 5, "Retrieve more: hash=aaa111bbb222"),
            generation(2, 6, 10, "Retrieve more: hash=ccc333ddd444"),
        ),
        updated_at="2026-08-06T00:00:00+00:00",
    )
    assert await store.compare_and_set_envelope("u", "s", 0, existing)
    await store.store_ccr_summary("u", "s", "aaa111bbb222", "old catalog entry")
    await store.store_ccr_summary("u", "s", "ccc333ddd444", "new catalog entry")
    job = compression_job(expected_version=1).model_copy(
        update={"evict_oldest_generation": True}
    )
    await worker.queue.enqueue(job)

    result = await worker.run_once()
    saved = await store.read_envelope("u", "s")

    assert result.state == "acked"
    assert saved is not None
    assert [item.generation for item in saved.compression_generations] == [2]
    assert await store.get_ccr_summaries("u", "s") == {
        "aaa111bbb222": "old catalog entry",
        "ccc333ddd444": "new catalog entry",
    }


@pytest.mark.asyncio
async def test_worker_publishes_completion_only_after_successful_cas_and_ack(worker):
    worker, store, _ = worker
    publisher = RecordingCompletionPublisher(worker.queue)
    worker.completion_publisher = publisher
    await worker.queue.enqueue(compression_job())

    result = await worker.run_once()

    assert result.state == "acked"
    assert len(publisher.calls) == 1
    published_job, published_envelope = publisher.calls[0]
    assert published_job.job_id == compression_job().job_id
    assert published_envelope == await store.read_envelope("u", "s")


@pytest.mark.asyncio
async def test_worker_does_not_publish_completion_for_retry(worker):
    worker, _, _ = worker
    publisher = RecordingCompletionPublisher()
    worker.completion_publisher = publisher
    worker.headroom = FailingHeadroom()
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "retry"
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_completion_publish_failure_does_not_rollback_stored_envelope(worker):
    worker, store, _ = worker
    worker.completion_publisher = FailingCompletionPublisher()
    await worker.queue.enqueue(compression_job())

    result = await worker.run_once()

    assert result.state == "acked"
    assert await store.read_envelope("u", "s") is not None


@pytest.mark.asyncio
async def test_explicit_rebuild_job_uses_journal_candidate_when_envelope_is_missing(worker):
    worker, store, _ = worker
    await worker.queue.enqueue(
        compression_job(through_sequence=10).model_copy(update={"rebuild": True})
    )

    result = await worker.run_once()
    persisted = await store.read_envelope("u", "s")

    assert result.state == "acked"
    assert persisted is not None
    assert persisted.compressed_through_sequence == 10


@pytest.mark.asyncio
async def test_in_process_waiter_completes_a_durable_rebuild_through_worker_boundary(worker):
    worker, _, _ = worker
    job = compression_job(through_sequence=10).model_copy(update={"rebuild": True})
    await worker.queue.enqueue(job)

    envelope = await InProcessRebuildWaiter(worker).wait_for(job, timeout_seconds=1)

    assert envelope is not None
    assert envelope.compressed_through_sequence == 10
    assert envelope.compression_generations[0].messages[0].content == "marker"


@pytest.mark.asyncio
async def test_in_process_waiter_continues_past_an_unrelated_queue_head():
    class Store:
        async def read_envelope(self, user_id, session_id):
            return envelope(version=2, through=10)

    class Worker:
        def __init__(self):
            self.store = Store()
            self.results = iter((
                CompressionWorkerResult("acked", job_id="other"),
                CompressionWorkerResult("acked", job_id="target"),
            ))

        async def run_once(self):
            return next(self.results)

    job = compression_job(through_sequence=10).model_copy(update={"job_id": "target"})

    result = await InProcessRebuildWaiter(Worker()).wait_for(job, timeout_seconds=1)

    assert result is not None and result.version == 2


@pytest.mark.asyncio
async def test_waiter_accepts_fresh_target_session_envelope_after_target_job_is_coalesced(worker):
    worker, _, _ = worker
    worker.queue.capacity = 1
    active = CompressionJob(
        job_id="active", user_id="other", session_id="other",
        expected_version=0, requested_through_sequence=1,
    )
    stronger = CompressionJob(
        job_id="stronger", user_id="u", session_id="s",
        expected_version=0, requested_through_sequence=10, rebuild=True,
    )
    target = CompressionJob(
        job_id="target", user_id="u", session_id="s",
        expected_version=0, requested_through_sequence=5, rebuild=True,
    )
    await worker.queue.enqueue(active)
    await worker.queue.enqueue(stronger)
    assert await worker.queue.enqueue(target) == "coalesced"
    active_lease = await worker.queue.lease("other-worker", now_unix_ms=0)
    assert active_lease is not None
    assert await worker.queue.ack(active_lease)

    envelope = await InProcessRebuildWaiter(worker).wait_for(target, timeout_seconds=0.05)

    assert envelope is not None
    assert envelope.version == 1
    assert envelope.compressed_through_sequence == 10


@pytest.mark.asyncio
async def test_waiter_accepts_concurrent_fresh_envelope_when_target_finishes_stale():
    class Store:
        async def read_envelope(self, user_id, session_id):
            return envelope(version=2, through=10)

    class Worker:
        store = Store()

        async def run_once(self):
            return CompressionWorkerResult("stale", job_id="target")

    job = compression_job(through_sequence=10).model_copy(update={"job_id": "target"})

    result = await InProcessRebuildWaiter(Worker()).wait_for(job, timeout_seconds=1)

    assert result is not None and result.version == 2


@pytest.mark.asyncio
async def test_waiter_returns_already_fresh_target_session_when_queue_is_idle():
    class Store:
        async def read_envelope(self, user_id, session_id):
            return envelope(version=2, through=10)

    class Worker:
        store = Store()

        async def run_once(self):
            return CompressionWorkerResult("idle")

    target = compression_job(through_sequence=10).model_copy(update={"job_id": "target"})

    result = await InProcessRebuildWaiter(Worker()).wait_for(target, timeout_seconds=0.05)

    assert result is not None and result.version == 2


@pytest.mark.asyncio
async def test_waiter_follows_durable_retry_until_transient_headroom_failure_recovers(worker):
    worker, _, _ = worker
    job = compression_job().model_copy(update={"rebuild": True})
    worker.headroom = FailOnceHeadroom()
    instants = iter(
        datetime(2026, 8, 6, 0, 0, second, tzinfo=timezone.utc)
        for second in range(0, 30, 2)
    )
    worker.clock = lambda: next(instants)
    await worker.queue.enqueue(job)

    rebuilt = await InProcessRebuildWaiter(worker).wait_for(job, timeout_seconds=1)

    assert rebuilt is not None and rebuilt.compressed_through_sequence == 10
    assert worker.headroom.calls == 2


@pytest.mark.asyncio
async def test_waiter_follows_deferred_lease_until_rebuild_succeeds(worker):
    worker, _, _ = worker
    job = compression_job().model_copy(update={"rebuild": True})
    acquire = worker.store.acquire_compression_lease
    calls = 0

    async def defer_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        return False if calls == 1 else await acquire(*args, **kwargs)

    worker.store.acquire_compression_lease = defer_once
    instants = iter(
        datetime(2026, 8, 6, 0, 0, second, tzinfo=timezone.utc)
        for second in range(0, 30, 2)
    )
    worker.clock = lambda: next(instants)
    await worker.queue.enqueue(job)

    rebuilt = await InProcessRebuildWaiter(worker).wait_for(job, timeout_seconds=1)

    assert rebuilt is not None and rebuilt.compressed_through_sequence == 10
    assert calls == 2


@pytest.mark.asyncio
async def test_waiter_keeps_polling_after_lost_until_another_worker_publishes():
    class Store:
        current = None

        async def read_envelope(self, user_id, session_id):
            return self.current

    class Worker:
        worker_concurrency = 1

        def __init__(self):
            self.store = Store()
            self.calls = 0

        async def run_once(self):
            self.calls += 1
            if self.calls == 2:
                self.store.current = envelope(version=1, through=10)
            return CompressionWorkerResult("lost", job_id="target")

    worker = Worker()
    job = compression_job().model_copy(update={"job_id": "target"})

    rebuilt = await InProcessRebuildWaiter(worker, poll_seconds=0.01).wait_for(
        job, timeout_seconds=0.1
    )

    assert rebuilt is not None
    assert worker.calls == 2


@pytest.mark.asyncio
async def test_waiter_transient_states_do_not_busy_spin():
    class Store:
        async def read_envelope(self, user_id, session_id):
            return None

    class Worker:
        worker_concurrency = 1
        store = Store()

        def __init__(self):
            self.calls = 0

        async def run_once(self):
            self.calls += 1
            return CompressionWorkerResult("lost", job_id="target")

    worker = Worker()
    with pytest.raises(TimeoutError):
        await InProcessRebuildWaiter(worker, poll_seconds=0.01).wait_for(
            compression_job().model_copy(update={"job_id": "target"}),
            timeout_seconds=0.035,
        )

    assert worker.calls <= 4


@pytest.mark.asyncio
async def test_waiter_limits_shared_worker_concurrency_and_releases_session_locks():
    class Store:
        async def read_envelope(self, user_id, session_id):
            return None

    class Worker:
        worker_concurrency = 2
        store = Store()

        def __init__(self):
            self.active = 0
            self.peak = 0

        async def run_once(self):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return CompressionWorkerResult("lost", job_id=None)
            finally:
                self.active -= 1

    worker = Worker()
    waiter = InProcessRebuildWaiter(worker)
    jobs = tuple(
        compression_job().model_copy(
            update={"job_id": f"job-{index}", "session_id": f"session-{index}"}
        )
        for index in range(8)
    )

    results = await asyncio.gather(
        *(waiter.wait_for(job, timeout_seconds=0.04) for job in jobs),
        return_exceptions=True,
    )

    assert all(isinstance(result, TimeoutError) for result in results)
    assert worker.peak == worker.worker_concurrency
    assert waiter.session_lock_count == 0


@pytest.mark.asyncio
async def test_stale_worker_is_acked_without_overwrite(worker):
    worker, store, _ = worker
    assert await store.compare_and_set_envelope("u", "s", 0, envelope(version=1))
    await worker.queue.enqueue(compression_job(expected_version=0))

    result = await worker.run_once()

    assert result.state == "stale"
    assert (await store.read_envelope("u", "s")).version == 1


@pytest.mark.asyncio
async def test_stale_activation_rebuild_requeues_against_new_envelope_version(worker):
    worker, store, _ = worker
    assert await store.compare_and_set_envelope("u", "s", 0, envelope(version=2))
    job = compression_job(through_sequence=10, expected_version=1).model_copy(
        update={"rebuild": True}
    )
    await worker.queue.enqueue(job)
    original_enqueue = worker.queue.enqueue
    worker.queue.enqueue = AsyncMock(wraps=original_enqueue)

    result = await worker.run_once()

    assert result.state == "stale"
    worker.queue.enqueue.assert_awaited_once()
    replacement = worker.queue.enqueue.await_args.args[0]
    assert replacement.expected_version == 2
    assert replacement.requested_through_sequence == 10
    assert replacement.rebuild is True
    assert replacement.job_id != job.job_id


@pytest.mark.asyncio
async def test_stale_rebuild_does_not_requeue_when_fresh_generation_covers_range(worker):
    worker, store, _ = worker
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    worker.clock = lambda: now
    covered = envelope(version=2, through=10, generations=(generation(1, 1, 10, "fresh"),))
    assert await store.compare_and_set_envelope("u", "s", 0, covered)
    job = compression_job(through_sequence=10, expected_version=1).model_copy(
        update={"rebuild": True}
    )
    await worker.queue.enqueue(job)
    original_enqueue = worker.queue.enqueue
    worker.queue.enqueue = AsyncMock(wraps=original_enqueue)

    result = await worker.run_once()

    assert result.state == "stale"
    worker.queue.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_reports_lost_when_successful_cas_cannot_ack(worker):
    worker, store, _ = worker
    worker.queue.ack = AsyncMock(return_value=False)
    await worker.queue.enqueue(compression_job())

    result = await worker.run_once()

    assert result.state == "lost"
    assert (await store.read_envelope("u", "s")) is not None


@pytest.mark.asyncio
async def test_worker_reports_lost_when_stale_ack_loses_ownership(worker):
    worker, store, _ = worker
    assert await store.compare_and_set_envelope("u", "s", 0, envelope(version=1))
    worker.queue.ack = AsyncMock(return_value=False)
    await worker.queue.enqueue(compression_job(expected_version=0))

    result = await worker.run_once()

    assert result.state == "lost"


@pytest.mark.asyncio
async def test_headroom_failure_retries_without_advancing_envelope(worker):
    worker, store, _ = worker
    worker.headroom = FailingHeadroom()
    await worker.queue.enqueue(compression_job())

    result = await worker.run_once()

    assert result.state == "retry"
    assert await store.read_envelope("u", "s") is None
    assert worker.queue.client.zsets[worker.queue.RETRY_KEY]


@pytest.mark.asyncio
async def test_run_forever_stops_idle_without_leasing_more_work(worker):
    worker, _, _ = worker
    stop = asyncio.Event()
    stop.set()
    worker.run_once = AsyncMock()

    await worker.run_forever(stop_event=stop, poll_seconds=0.01)

    worker.run_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_forever_finishes_active_job_then_stops_before_next_lease(worker):
    worker, _, _ = worker
    stop = asyncio.Event()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def active_once():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return CompressionWorkerResult("acked", "job")

    worker.run_once = active_once
    running = asyncio.create_task(
        worker.run_forever(stop_event=stop, poll_seconds=0.01)
    )
    await entered.wait()
    stop.set()
    release.set()

    await asyncio.wait_for(running, timeout=0.2)
    assert calls == 1


@pytest.mark.asyncio
async def test_run_forever_propagates_fatal_worker_error(worker):
    worker, _, _ = worker

    async def fatal():
        raise RuntimeError("fatal worker failure")

    worker.run_once = fatal

    with pytest.raises(RuntimeError, match="fatal worker failure"):
        await worker.run_forever(stop_event=asyncio.Event(), poll_seconds=0.01)


@pytest.mark.asyncio
async def test_forced_cancellation_returns_queue_and_session_leases_immediately(worker):
    worker, store, _ = worker
    blocking = BlockingHeadroom()
    worker.headroom = blocking
    target = compression_job()
    await worker.queue.enqueue(target)
    running = asyncio.create_task(worker.run_once())
    await blocking.started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    reclaimed = await worker.queue.lease(
        "replacement", now_unix_ms=int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    assert reclaimed is not None and reclaimed.job == target
    assert await store.acquire_compression_lease("u", "s", "replacement") is True


@pytest.mark.asyncio
async def test_cancelling_run_forever_returns_active_loop_lease(worker):
    worker, _, _ = worker
    blocking = BlockingHeadroom()
    worker.headroom = blocking
    target = compression_job()
    await worker.queue.enqueue(target)
    running = asyncio.create_task(worker.run_forever(poll_seconds=0.01))
    await blocking.started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    reclaimed = await worker.queue.lease(
        "replacement", now_unix_ms=int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    assert reclaimed is not None and reclaimed.job == target


@pytest.mark.asyncio
async def test_headroom_failure_uses_fresh_clock_for_retry_deadline(worker):
    worker, _, _ = worker
    t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
    t10 = datetime(2026, 8, 6, 0, 0, 10, tzinfo=timezone.utc)
    clock_values = iter((t0, t10))
    worker.clock = lambda: next(clock_values)
    worker.headroom = FailingHeadroom()
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "retry"
    due = worker.queue.client.zsets[worker.queue.RETRY_KEY]["job-10-0"]
    assert due == int(t10.timestamp() * 1_000) + 1_000


@pytest.mark.asyncio
async def test_deferred_session_lease_uses_fresh_clock_for_retry_deadline(worker):
    worker, _, _ = worker
    t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
    t10 = datetime(2026, 8, 6, 0, 0, 10, tzinfo=timezone.utc)
    clock_values = iter((t0, t10))
    worker.clock = lambda: next(clock_values)
    worker.store.acquire_compression_lease = AsyncMock(return_value=False)
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "deferred"
    due = worker.queue.client.zsets[worker.queue.RETRY_KEY]["job-10-0"]
    assert due == int(t10.timestamp() * 1_000) + 1_000


@pytest.mark.asyncio
async def test_generation_timestamps_use_the_headroom_completion_clock(worker):
    worker, store, _ = worker
    t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
    t10 = datetime(2026, 8, 6, 0, 0, 10, tzinfo=timezone.utc)
    clock_values = iter((t0, t10))
    worker.clock = lambda: next(clock_values)
    worker.headroom = DelayedSuccessfulHeadroom()
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "acked"
    generation = (await store.read_envelope("u", "s")).compression_generations[0]
    assert generation.created_at == t10.isoformat()
    assert generation.ccr_expires_at == "2026-08-06T12:00:10+00:00"


@pytest.mark.asyncio
async def test_worker_reports_lost_when_headroom_retry_loses_ownership(worker):
    worker, _, _ = worker
    worker.headroom = FailingHeadroom()
    worker.queue.retry = AsyncMock(return_value="lost")
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "lost"


@pytest.mark.asyncio
async def test_cancelled_worker_job_is_reclaimed_after_its_queue_lease_expires(worker):
    worker, _, _ = worker
    blocking = BlockingHeadroom()
    worker.headroom = blocking
    fixed_now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    worker.clock = lambda: fixed_now
    await worker.queue.enqueue(compression_job())
    task = asyncio.create_task(worker.run_once())
    await blocking.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    reclaimed = await worker.queue.lease(
        "replacement", now_unix_ms=int(fixed_now.timestamp() * 1_000) + 300_001
    )
    assert reclaimed is not None and reclaimed.job.job_id == "job-10-0"

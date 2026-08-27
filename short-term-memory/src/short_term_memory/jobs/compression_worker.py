"""Durable original-only Headroom compression worker."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid
from typing import AsyncIterator, Callable

from short_term_memory.compression.async_headroom_client import AsyncHeadroomClient
from short_term_memory.compression.ccr_recall import extract_marker_hashes
from short_term_memory.compression.generations import CompressionCandidate, GenerationPlanner
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.jobs.redis_compression_queue import (
    CompressionJob,
    CompressionJobLease,
    RedisCompressionQueue,
)
from short_term_memory.models import (
    CompressionGeneration,
    HeadroomCompressionStatus,
    MemorySummaryEnvelope,
)


@dataclass(frozen=True)
class CompressionWorkerResult:
    state: str
    job_id: str | None = None


class CompressionWorker:
    def __init__(
        self,
        *,
        queue: RedisCompressionQueue,
        store: object,
        planner: GenerationPlanner,
        headroom: AsyncHeadroomClient,
        compression_model: str,
        scope_factory: OptimizationScopeFactory,
        ccr_ttl_seconds: int,
        ccr_refresh_seconds: int,
        max_segments: int,
        retain_budget: int = 0,
        worker_concurrency: int = 1,
        completion_publisher: object | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if min(ccr_ttl_seconds, ccr_refresh_seconds, max_segments, worker_concurrency) < 1:
            raise ValueError("worker limits must be positive")
        self.queue = queue
        self.store = store
        self.planner = planner
        self.headroom = headroom
        self.compression_model = compression_model
        self.scope_factory = scope_factory
        self.ccr_ttl_seconds = ccr_ttl_seconds
        self.ccr_refresh_seconds = ccr_refresh_seconds
        self.max_segments = max_segments
        self.retain_budget = retain_budget
        self.worker_concurrency = worker_concurrency
        self.completion_publisher = completion_publisher
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run_once(self) -> CompressionWorkerResult:
        now = self._now()
        lease = await self.queue.lease(uuid.uuid4().hex, now_unix_ms=self._unix_ms(now))
        if lease is None:
            return CompressionWorkerResult("idle")
        session_token = uuid.uuid4().hex
        acquired = False
        try:
            acquired = await self.store.acquire_compression_lease(
                lease.job.user_id, lease.job.session_id, session_token
            )
            if not acquired:
                return await self._retry(lease, "deferred")
            return await self._execute(lease, now)
        except asyncio.CancelledError:
            await self._return_cancelled_lease(lease, session_token)
            acquired = False
            raise
        except Exception:
            return await self._retry(lease, "retry")
        finally:
            if acquired:
                await asyncio.shield(
                    self.store.release_compression_lease(
                        lease.job.user_id, lease.job.session_id, session_token
                    )
                )

    async def run_forever(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        poll_seconds: float = 0.1,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        stopping = stop_event or asyncio.Event()

        async def loop() -> None:
            while not stopping.is_set():
                result = await self.run_once()
                if result.state == "idle":
                    try:
                        async with asyncio.timeout(poll_seconds):
                            await stopping.wait()
                    except TimeoutError:
                        pass

        tasks = [asyncio.create_task(loop()) for _ in range(self.worker_concurrency)]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            error = next(
                (
                    task.exception()
                    for task in done
                    if not task.cancelled() and task.exception() is not None
                ),
                None,
            )
            if error is not None:
                raise error
            await asyncio.gather(*pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(
        self, lease: CompressionJobLease, now: datetime
    ) -> CompressionWorkerResult:
        job = lease.job
        envelope = await self.store.read_envelope(job.user_id, job.session_id)
        current_version = envelope.version if envelope is not None else 0
        if current_version != job.expected_version:
            return await self._ack_stale_rebuild(lease, envelope, now)

        # Storage-pressure eviction is deliberately separate from Claude L2/L3/L4.
        if job.evict_oldest_generation:
            return await self._execute_evict_oldest_generation(lease, envelope, now)

        candidate = await self._candidate(job, envelope, now)
        if candidate is None or candidate.expected_version != job.expected_version:
            latest = await self.store.read_envelope(job.user_id, job.session_id)
            return await self._ack_stale_rebuild(lease, latest, now)
        if not candidate.originals:
            return await self._ack(lease, "acked")

        # Headroom input is constructed exclusively from the selected journal
        # originals.  No summary envelope or prior generation is ever included.
        messages = tuple(
            {"role": event.role.value, "content": event.content}
            for event in candidate.originals
        )
        compressed = await self.headroom.compress(
            messages,
            model=self.compression_model,
            correlation_id=job.job_id,
            scope_headers=self.scope_factory.for_session(
                job.user_id, job.session_id
            ).as_headroom_headers(),
        )
        if compressed.status is not HeadroomCompressionStatus.SUCCESS:
            return await self._retry(lease, "retry")

        completed_at = self._now()
        next_envelope = self._next_envelope(
            envelope, candidate, compressed, completed_at
        )
        written = await self.store.compare_and_set_envelope(
            job.user_id, job.session_id, job.expected_version, next_envelope
        )
        if not written:
            latest = await self.store.read_envelope(job.user_id, job.session_id)
            return await self._ack_stale_rebuild(lease, latest, self._now())
        # Record hash -> content summary for each marker so recall can match by query.
        try:
            await self._record_ccr_summaries(
                job.user_id, job.session_id, compressed.messages
            )
        except Exception:
            # Recording summaries is an optimization; failure must not fail the ack.
            pass
        # Shrink the online context: drop originals already covered by compression.
        try:
            await self.store.trim_originals(
                job.user_id,
                job.session_id,
                next_envelope.compressed_through_sequence,
                retain_budget=self.retain_budget,
            )
        except Exception:
            # Trimming is an optimization; a failure must not fail the ack.
            pass
        return await self._ack(
            lease, "acked", completed_envelope=next_envelope
        )

    async def _execute_evict_oldest_generation(
        self, lease, envelope: MemorySummaryEnvelope | None, now: datetime
    ) -> CompressionWorkerResult:
        """Shrink the context by dropping the OLDEST compressed generation.

        When the whole context is already compressed but still over threshold, we
        remove the oldest generation from the summary.  Its marker hashes remain in
        the separate ``ccr-summaries`` map, so recall can still fetch the original
        text from the CCR cache (within its TTL).
        """
        job = lease.job
        if envelope is None or len(envelope.compression_generations) <= 1:
            return await self._ack(lease, "acked")

        old_generations = envelope.compression_generations
        # Drop the oldest (first) generation; keep the rest.
        kept = old_generations[1:]

        next_envelope = envelope.model_copy(
            update={
                "version": envelope.version + 1,
                "compression_generations": kept,
                "updated_at": now.isoformat(),
            }
        )
        written = await self.store.compare_and_set_envelope(
            job.user_id, job.session_id, job.expected_version, next_envelope
        )
        if not written:
            return await self._ack(lease, "stale")
        return await self._ack(lease, "acked", completed_envelope=next_envelope)

    async def _record_ccr_summaries(
        self, user_id: str, session_id: str, compressed_messages: tuple[dict, ...]
    ) -> None:
        """Store a content snippet for every marker hash in the compressed result."""
        hashes = extract_marker_hashes(compressed_messages)
        if not hashes:
            return
        # Use the compressed messages text as the summary source.
        text = "\n".join(
            str(m.get("content", "")) for m in compressed_messages if isinstance(m, dict)
        )
        summary = text[:300]
        for hash_value in hashes:
            await self.store.store_ccr_summary(
                user_id, session_id, hash_value, summary
            )

    async def _candidate(
        self, job, envelope: MemorySummaryEnvelope | None, now: datetime
    ) -> CompressionCandidate | None:
        if job.rebuild:
            return await self.planner.plan_rebuild(
                job.user_id, job.session_id, job.requested_through_sequence
            )
        candidate = await self.planner.plan_incremental(job.user_id, job.session_id)
        if candidate is None:
            return None
        originals = tuple(
            event
            for event in candidate.originals
            if event.sequence <= job.requested_through_sequence
        )
        if not originals:
            return None
        return CompressionCandidate(
            user_id=candidate.user_id,
            session_id=candidate.session_id,
            expected_version=candidate.expected_version,
            from_sequence=originals[0].sequence,
            through_sequence=originals[-1].sequence,
            originals=originals,
            rebuild=False,
        )

    def _next_envelope(
        self,
        current: MemorySummaryEnvelope | None,
        candidate: CompressionCandidate,
        compressed,
        now: datetime,
    ) -> MemorySummaryEnvelope:
        previous = current.compression_generations if current is not None else ()
        generation = CompressionGeneration(
            generation=max((item.generation for item in previous), default=0) + 1,
            from_sequence=candidate.from_sequence,
            through_sequence=candidate.through_sequence,
            messages=compressed.messages,
            tokens_before=compressed.tokens_before or 0,
            tokens_after=compressed.tokens_after or 0,
            created_at=now.isoformat(),
            ccr_expires_at=(now + timedelta(seconds=self.ccr_ttl_seconds)).isoformat(),
        )
        generations = (generation,) if candidate.rebuild else (*previous, generation)
        if current is not None:
            return current.model_copy(
                update={
                    "version": candidate.expected_version + 1,
                    "compressed_through_sequence": candidate.through_sequence,
                    "compression_generations": generations,
                    "updated_at": now.isoformat(),
                }
            )
        return MemorySummaryEnvelope(
            version=candidate.expected_version + 1,
            compressed_through_sequence=candidate.through_sequence,
            compression_generations=generations,
            updated_at=now.isoformat(),
        )

    def _now(self) -> datetime:
        return self._aware_datetime(self.clock())

    @staticmethod
    def _aware_datetime(value: datetime | str) -> datetime:
        result = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("worker clock and CCR timestamps must be timezone-aware")
        return result

    @staticmethod
    def _unix_ms(value: datetime) -> int:
        return int(value.timestamp() * 1_000)

    async def _retry(
        self, lease: CompressionJobLease, state: str
    ) -> CompressionWorkerResult:
        retry_now = self._now()
        result = await self.queue.retry(lease, now_unix_ms=self._unix_ms(retry_now))
        if result == "dead":
            return CompressionWorkerResult("dead", lease.job.job_id)
        if result == "lost":
            return CompressionWorkerResult("lost", lease.job.job_id)
        return CompressionWorkerResult(state, lease.job.job_id)

    async def _ack_stale_rebuild(
        self,
        lease: CompressionJobLease,
        envelope: MemorySummaryEnvelope | None,
        now: datetime,
    ) -> CompressionWorkerResult:
        job = lease.job
        if (
            job.rebuild
            and not job.evict_oldest_generation
            and not self._has_fresh_rebuild_coverage(envelope, job, now)
        ):
            current_version = envelope.version if envelope is not None else 0
            await self.queue.enqueue(job.rebased(expected_version=current_version))
        return await self._ack(lease, "stale")

    def _has_fresh_rebuild_coverage(
        self,
        envelope: MemorySummaryEnvelope | None,
        job: CompressionJob,
        now: datetime,
    ) -> bool:
        if envelope is None:
            return False
        return any(
            generation.from_sequence == 1
            and generation.through_sequence >= job.requested_through_sequence
            and self._aware_datetime(generation.ccr_expires_at) > now
            for generation in envelope.compression_generations
        )

    async def _return_cancelled_lease(
        self, lease: CompressionJobLease, session_token: str
    ) -> None:
        now = self._now()
        operations = asyncio.gather(
            self.queue.return_lease(
                lease, now_unix_ms=self._unix_ms(now)
            ),
            self.store.release_compression_lease(
                lease.job.user_id, lease.job.session_id, session_token
            ),
            return_exceptions=True,
        )
        await asyncio.shield(operations)

    async def _ack(
        self,
        lease: CompressionJobLease,
        state: str,
        *,
        completed_envelope: MemorySummaryEnvelope | None = None,
    ) -> CompressionWorkerResult:
        if not await self.queue.ack(lease):
            return CompressionWorkerResult("lost", lease.job.job_id)
        if (
            state == "acked"
            and completed_envelope is not None
            and self.completion_publisher is not None
        ):
            try:
                await self.completion_publisher.publish(
                    lease.job, completed_envelope
                )
            except Exception:
                # The envelope is already durable and the lease is ACKed.  A
                # waiter periodically checks the envelope when notification is
                # unavailable, so transport failure must not roll back success.
                pass
        return CompressionWorkerResult(state, lease.job.job_id)


class InProcessRebuildWaiter:
    """Explicit worker-service boundary for a bounded cold-rebuild wait."""

    def __init__(
        self, worker: CompressionWorker, *, poll_seconds: float = 0.01
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.worker = worker
        self.poll_seconds = poll_seconds
        self._locks: dict[tuple[str, str], _WaitLock] = {}
        self._worker_slots = asyncio.Semaphore(
            max(1, getattr(worker, "worker_concurrency", 1))
        )

    @property
    def session_lock_count(self) -> int:
        return len(self._locks)

    async def wait_for(self, job, timeout_seconds: float) -> MemorySummaryEnvelope | None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        async with asyncio.timeout(timeout_seconds):
            async with self._session_lock(job.user_id, job.session_id):
                while True:
                    envelope = await self.worker.store.read_envelope(
                        job.user_id, job.session_id
                    )
                    if self._matches(job, envelope):
                        return envelope
                    async with self._worker_slots:
                        result = await self.worker.run_once()
                    envelope = await self.worker.store.read_envelope(
                        job.user_id, job.session_id
                    )
                    if self._matches(job, envelope):
                        return envelope
                    if result.job_id == job.job_id and result.state in {
                        "acked",
                        "dead",
                        "stale",
                    }:
                        return None
                    await asyncio.sleep(self.poll_seconds)

    @asynccontextmanager
    async def _session_lock(
        self, user_id: str, session_id: str
    ) -> AsyncIterator[None]:
        key = (user_id, session_id)
        entry = self._locks.get(key)
        if entry is None:
            entry = _WaitLock(asyncio.Lock())
            self._locks[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(key) is entry:
                self._locks.pop(key, None)

    @staticmethod
    def _matches(job, envelope: MemorySummaryEnvelope | None) -> bool:
        return bool(
            envelope is not None
            and envelope.version > job.expected_version
            and envelope.compressed_through_sequence >= job.requested_through_sequence
        )


@dataclass
class _WaitLock:
    lock: asyncio.Lock
    users: int = 0

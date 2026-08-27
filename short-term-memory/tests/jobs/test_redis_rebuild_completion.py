import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.jobs.redis_compression_queue import CompressionJob
from short_term_memory.jobs.redis_rebuild_completion import RedisRebuildCompletion
from short_term_memory.models import CompressionGeneration
from tests.factories import envelope


class CompletionRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


class EnvelopeStore:
    def __init__(self) -> None:
        self.current = None
        self.reads = 0

    async def read_envelope(self, user_id: str, session_id: str):
        self.reads += 1
        return self.current


def job(*, job_id: str = "target", through: int = 10) -> CompressionJob:
    return CompressionJob(
        job_id=job_id,
        user_id="private-user",
        session_id="private-session",
        expected_version=0,
        requested_through_sequence=through,
        rebuild=True,
    )


def fresh_envelope(*, through: int = 10):
    now = datetime.now(timezone.utc)
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=through,
        messages=({"role": "system", "content": "opaque-marker"},),
        tokens_before=100,
        tokens_after=25,
        created_at=now.isoformat(),
        ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    return envelope(version=1, through=through, generations=[generation])


def completion(redis: CompletionRedis, store: EnvelopeStore, **kwargs):
    return RedisRebuildCompletion(
        redis,
        store=store,
        scope_factory=OptimizationScopeFactory("test-secret"),
        ttl_seconds=60,
        poll_seconds=0.01,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_completion_before_waiter_start_is_observable_without_job_content() -> None:
    redis = CompletionRedis()
    store = EnvelopeStore()
    store.current = fresh_envelope()
    transport = completion(redis, store)

    await transport.publish(job(), store.current)
    result = await transport.wait_for(job(), timeout_seconds=0.1)

    assert result == store.current
    assert list(redis.expirations.values()) == [60]
    key, payload = next(iter(redis.values.items()))
    assert "private-user" not in key
    assert "private-session" not in key
    assert "opaque-marker" not in payload
    assert json.loads(payload) == {"version": 1, "through_sequence": 10}


@pytest.mark.asyncio
async def test_waiter_observes_completion_published_after_it_starts() -> None:
    redis = CompletionRedis()
    store = EnvelopeStore()
    transport = completion(redis, store)
    waiting = asyncio.create_task(transport.wait_for(job(), timeout_seconds=0.2))
    await asyncio.sleep(0.02)

    store.current = fresh_envelope()
    await transport.publish(job(), store.current)

    assert await waiting == store.current


@pytest.mark.asyncio
async def test_coalesced_job_id_matches_fresh_session_coverage_not_marker_job_id() -> None:
    redis = CompletionRedis()
    store = EnvelopeStore()
    store.current = fresh_envelope(through=20)
    transport = completion(redis, store)

    await transport.publish(job(job_id="coalesced-worker", through=20), store.current)
    result = await transport.wait_for(
        job(job_id="discarded-target", through=10), timeout_seconds=0.1
    )

    assert result == store.current


@pytest.mark.asyncio
async def test_waiter_periodically_rechecks_envelope_when_notification_is_lost() -> None:
    redis = CompletionRedis()
    store = EnvelopeStore()
    transport = completion(redis, store)
    waiting = asyncio.create_task(transport.wait_for(job(), timeout_seconds=0.2))
    await asyncio.sleep(0.02)

    store.current = fresh_envelope()

    assert await waiting == store.current
    assert redis.values == {}
    assert store.reads >= 2


@pytest.mark.asyncio
async def test_timeout_and_concurrent_waiters_do_not_busy_loop() -> None:
    redis = CompletionRedis()
    store = EnvelopeStore()
    transport = completion(redis, store)

    results = await asyncio.gather(
        *(transport.wait_for(job(), timeout_seconds=0.04) for _ in range(3))
    )

    assert results == [None, None, None]
    assert 3 <= store.reads < 30


@pytest.mark.asyncio
async def test_total_timeout_interrupts_a_blocked_envelope_read() -> None:
    class BlockingStore:
        async def read_envelope(self, user_id, session_id):
            await asyncio.Event().wait()

    transport = completion(CompletionRedis(), BlockingStore())

    started = asyncio.get_running_loop().time()
    assert await transport.wait_for(job(), timeout_seconds=0.03) is None
    assert asyncio.get_running_loop().time() - started < 0.15


@pytest.mark.asyncio
async def test_marker_failure_does_not_stop_bounded_envelope_polling() -> None:
    class BrokenMarkerRedis(CompletionRedis):
        async def get(self, key):
            raise ConnectionError("notification channel failed")

    store = EnvelopeStore()
    transport = completion(BrokenMarkerRedis(), store)
    waiting = asyncio.create_task(transport.wait_for(job(), timeout_seconds=0.2))
    await asyncio.sleep(0.03)
    store.current = fresh_envelope()

    assert await waiting == store.current


@pytest.mark.asyncio
async def test_external_cancellation_is_not_converted_to_timeout() -> None:
    class BlockingStore:
        async def read_envelope(self, user_id, session_id):
            await asyncio.Event().wait()

    waiting = asyncio.create_task(
        completion(CompletionRedis(), BlockingStore()).wait_for(
            job(), timeout_seconds=10
        )
    )
    await asyncio.sleep(0)
    waiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiting

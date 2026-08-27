import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import redis.asyncio as redis_async

from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.jobs.redis_compression_queue import CompressionJob
from short_term_memory.models import (
    CompressionGeneration,
    HeadroomCompressionResult,
    HeadroomCompressionStatus,
    HeadroomFailureReason,
)
from short_term_memory.service.runtime import ServiceRuntime, create_runtime_app
from tests.factories import envelope, memory_event


pytestmark = pytest.mark.skipif(
    os.environ.get("SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION") != "1",
    reason="set SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1 for Redis integration",
)


class HealthyHeadroomHttp:
    is_closed = False

    async def get(self, url: str, **kwargs):
        request = httpx.Request("GET", url)
        return httpx.Response(200, json={"status": "ok"}, request=request)

    async def post(self, *args, **kwargs):
        raise AssertionError("this integration does not trigger compression")

    async def aclose(self):
        self.is_closed = True


class FailOnceHeadroom:
    def __init__(self) -> None:
        self.calls = 0

    async def compress(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return HeadroomCompressionResult(
                status=HeadroomCompressionStatus.FAILED,
                messages=(),
                fallback_used=False,
                failure_reason=HeadroomFailureReason.SERVICE_UNAVAILABLE,
            )
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.SUCCESS,
            messages=({"role": "system", "content": "marker"},),
            fallback_used=False,
            tokens_before=100,
            tokens_after=25,
        )


def settings(tmp_path: Path, redis_url: str) -> ShortTermMemorySettings:
    base = ShortTermMemorySettings(home=str(tmp_path), environment="production")
    return replace(
        base,
        redis_session=replace(base.redis_session, url=redis_url),
        api=replace(base.api, auth_token="redis-integration-token"),
        headroom_service=replace(
            base.headroom_service, url="http://headroom.invalid:8787"
        ),
    )


def fresh_envelope(through: int):
    now = datetime.now(timezone.utc)
    return envelope(
        version=1,
        through=through,
        generations=[
            CompressionGeneration(
                generation=1,
                from_sequence=1,
                through_sequence=through,
                messages=({"role": "system", "content": "marker"},),
                tokens_before=100,
                tokens_after=25,
                created_at=now.isoformat(),
                ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
            )
        ],
    )


async def cleanup_queue_job(client, queue, job: CompressionJob) -> None:
    job_id = queue._job_component(job.job_id)
    session = queue._session_key(job)
    await client.lrem(queue.READY_KEY, 0, job_id)
    await client.srem(queue.READY_MEMBERS_KEY, job_id)
    await client.srem(queue.PENDING_KEY, session)
    await client.zrem(
        queue.INFLIGHT_KEY,
        job_id,
    )
    await client.zrem(queue.RETRY_KEY, job_id)
    await client.zrem(queue.DEAD_KEY, job_id)
    await client.zrem(queue.CORRUPT_KEY, job_id)
    await client.delete(
        queue._job_key(job.job_id),
        queue._lease_key(job.job_id),
        f"{queue.PENDING_PREFIX}{session}",
    )


@pytest.mark.asyncio
async def test_real_redis_http_write_then_read_returns_original_from_redis(
    tmp_path,
) -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    client = redis_async.Redis.from_url(redis_url, decode_responses=True)
    await client.ping()  # Explicit enablement must fail, never skip, if Redis is down.
    headroom_http = HealthyHeadroomHttp()
    runtime = await ServiceRuntime.start(
        settings(tmp_path, redis_url),
        redis=client,
        headroom_http=headroom_http,
    )
    user_id = f"http-it-{uuid4().hex}"
    session_id = "session"

    async def start(_settings):
        return runtime

    app = create_runtime_app(settings(tmp_path, redis_url), runtime_start=start)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as http:
                written = await http.post(
                    "/v1/memories/write",
                    headers={"authorization": "Bearer redis-integration-token"},
                    json={
                        "user_id": user_id,
                        "session_id": session_id,
                        "session_seconds": 0,
                        "events": [
                            {
                                "event_id": "event-1",
                                "role": "user",
                                "content_type": "conversation",
                                "content": "REAL_REDIS_HTTP_ORIGINAL",
                                "metadata": {},
                            }
                        ],
                    },
                )
                read = await http.post(
                    "/v1/memories/read",
                    headers={"authorization": "Bearer redis-integration-token"},
                    json={"user_id": user_id, "session_id": session_id},
                )

                assert written.status_code == 200
                assert read.status_code == 200
                assert read.json()["memory"]["source"] == "redis"
                assert any(
                    message.get("content") == "REAL_REDIS_HTTP_ORIGINAL"
                    for message in read.json()["messages"]
                )
    finally:
        queued = runtime.memory_service._compression_job(
            user_id, session_id, None, 1, rebuild=True
        )
        await cleanup_queue_job(client, runtime.queue, queued)
        cleanup = f"dream:session:{user_id}:{session_id}:*"
        keys = await client.keys(cleanup)
        if keys:
            await client.delete(*keys)
        await runtime.close()
        await client.aclose()
        await headroom_http.aclose()


@pytest.mark.asyncio
async def test_real_redis_completion_before_after_and_concurrent_waiters(
    tmp_path,
) -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    client = redis_async.Redis.from_url(redis_url, decode_responses=True)
    await client.ping()
    headroom_http = HealthyHeadroomHttp()
    runtime = await ServiceRuntime.start(
        settings(tmp_path, redis_url),
        redis=client,
        headroom_http=headroom_http,
    )
    target = CompressionJob(
        job_id=f"target-{uuid4().hex}",
        user_id=f"completion-it-{uuid4().hex}",
        session_id="session",
        expected_version=0,
        requested_through_sequence=10,
        rebuild=True,
    )
    stronger = target.model_copy(
        update={"job_id": f"coalesced-{uuid4().hex}", "requested_through_sequence": 20}
    )
    summary_key = runtime.store._keys(target.user_id, target.session_id).summary
    completion_key = runtime.completion._key(target)
    await client.delete(summary_key, completion_key)
    try:
        assert await runtime.completion.wait_for(target, 0.05) is None

        first = fresh_envelope(through=20)
        assert await runtime.store.compare_and_set_envelope(
            target.user_id, target.session_id, 0, first
        )
        await runtime.completion.publish(stronger, first)
        assert await runtime.completion.wait_for(target, 0.2) == first

        await client.delete(summary_key, completion_key)
        waiters = [
            asyncio.create_task(runtime.completion.wait_for(target, 0.5))
            for _ in range(3)
        ]
        await asyncio.sleep(0.05)
        assert await runtime.store.compare_and_set_envelope(
            target.user_id, target.session_id, 0, first
        )
        await runtime.completion.publish(stronger, first)
        assert await asyncio.gather(*waiters) == [first, first, first]
    finally:
        await client.delete(summary_key, completion_key)
        await runtime.close()
        await client.aclose()
        await headroom_http.aclose()


@pytest.mark.asyncio
async def test_real_redis_worker_retry_only_notifies_after_eventual_ack(tmp_path) -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    client = redis_async.Redis.from_url(redis_url, decode_responses=True)
    await client.ping()
    headroom_http = HealthyHeadroomHttp()
    runtime = await ServiceRuntime.start(
        settings(tmp_path, redis_url), redis=client, headroom_http=headroom_http
    )
    user_id = f"worker-retry-it-{uuid4().hex}"
    session_id = "session"
    for sequence in (1, 2):
        original = memory_event(
            sequence=sequence,
            event_id=f"event-{sequence}",
            content=f"original-{sequence}",
        )
        reservation = await runtime.store.reserve_event(
            user_id, session_id, original.event_id, original.sha256
        )
        original = original.model_copy(update={"sequence": reservation.sequence})
        runtime.memory_service.journals.append_event(user_id, session_id, original)
        await runtime.store.commit_event(user_id, session_id, original)

    target = CompressionJob(
        job_id=f"retry-{uuid4().hex}",
        user_id=user_id,
        session_id=session_id,
        expected_version=0,
        requested_through_sequence=2,
        rebuild=True,
    )
    completion_key = runtime.completion._key(target)
    headroom = FailOnceHeadroom()
    runtime.worker.headroom = headroom
    base = datetime.now(timezone.utc)
    instants = iter(base + timedelta(seconds=seconds) for seconds in (0, 2, 4, 6))
    runtime.worker.clock = lambda: next(instants)
    await runtime.queue.enqueue(target)
    try:
        assert (await runtime.worker.run_once()).state == "retry"
        assert await client.get(completion_key) is None

        assert (await runtime.worker.run_once()).state == "acked"
        assert await client.get(completion_key) is not None
        assert headroom.calls == 2
    finally:
        await cleanup_queue_job(client, runtime.queue, target)
        session_keys = await client.keys(f"dream:session:{user_id}:{session_id}:*")
        if session_keys:
            await client.delete(*session_keys)
        await client.delete(completion_key)
        await runtime.close()
        await client.aclose()
        await headroom_http.aclose()

"""Opt-in verification of the durable queue's Lua transitions against Redis."""

import os
from uuid import uuid4

import pytest

from short_term_memory.jobs.redis_compression_queue import CompressionJob, RedisCompressionQueue


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REDIS_QUEUE_INTEGRATION") != "1",
    reason="set RUN_REDIS_QUEUE_INTEGRATION=1 to run against a disposable Redis DB",
)


@pytest.mark.asyncio
async def test_redis_lua_recovers_expired_lease_promotes_pending_and_protects_token():
    import redis.asyncio as redis

    client = redis.Redis.from_url(
        os.environ.get("REDIS_QUEUE_TEST_URL", "redis://127.0.0.1:6379/15"),
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        raise

    first_id, second_id = f"it-{uuid4().hex}", f"it-{uuid4().hex}"
    queue = RedisCompressionQueue(client, capacity=1, lease_seconds=1)
    cleanup = [
        queue.READY_KEY,
        queue.READY_MEMBERS_KEY,
        queue.INFLIGHT_KEY,
        queue.RETRY_KEY,
        queue.PENDING_KEY,
        queue.DEAD_KEY,
        queue.CORRUPT_KEY,
        queue._job_key(first_id),
        queue._job_key(second_id),
        queue._lease_key(first_id),
        queue._lease_key(second_id),
        f"{queue.PENDING_PREFIX}1:u:1:s",
    ]
    await client.delete(*cleanup)
    try:
        first = CompressionJob(
            job_id=first_id, user_id="u", session_id="s",
            expected_version=0, requested_through_sequence=1,
        )
        second = first.model_copy(update={"job_id": second_id, "requested_through_sequence": 2})
        assert await queue.enqueue(first) == "ready"
        assert await queue.enqueue(second) == "pending"
        abandoned = await queue.lease("first-owner", now_unix_ms=0)
        assert abandoned is not None

        reclaimed = await queue.lease("second-owner", now_unix_ms=1_001)
        assert reclaimed is not None and reclaimed.job.job_id == first_id
        assert await queue.ack(abandoned) is False
        assert await queue.ack(reclaimed) is True

        promoted = await queue.lease("third-owner", now_unix_ms=1_002)
        assert promoted is not None and promoted.job.job_id == second_id
    finally:
        await client.delete(*cleanup)
        await client.aclose()

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
import redis.asyncio as redis

from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from tests.factories import memory_event


pytestmark = pytest.mark.skipif(
    os.environ.get("SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION") != "1",
    reason="set SHORT_TERM_MEMORY_RUN_REDIS_INTEGRATION=1 for Redis integration",
)


@pytest_asyncio.fixture
async def memory_store():
    client = redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        decode_responses=True,
    )
    try:
        yield AsyncRedisMemoryStore(client)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_real_redis_reserve_pending_and_commit_cleanup(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    user_id = f"redis-pending-{uuid4().hex}"
    session_id = "session"
    event = memory_event(event_id="event", content="pending lifecycle")
    prefix = f"dream:session:{user_id}:{session_id}"

    reservation = await memory_store.reserve_event(
        user_id, session_id, event.event_id, event.sha256
    )

    pending = await memory_store.client.smembers(f"{prefix}:pending-reservations")
    assert pending == {event.event_id}

    committed = event.model_copy(update={"sequence": reservation.sequence})
    assert await memory_store.commit_event(user_id, session_id, committed) == "committed"
    assert await memory_store.client.smembers(f"{prefix}:pending-reservations") == set()


@pytest.mark.asyncio
async def test_real_redis_reservations_are_atomic_under_concurrency(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    user_id = f"redis-test-{uuid4().hex}"
    session_id = "session"
    distinct_events = tuple(
        memory_event(event_id=f"event-{index}", content=f"content-{index}")
        for index in range(100)
    )
    reservations = await asyncio.gather(
        *(
            memory_store.reserve_event(
                user_id, session_id, event.event_id, event.sha256
            )
            for event in distinct_events
        )
    )

    assert sorted(reservation.sequence for reservation in reservations) == list(
        range(1, 101)
    )

    event = memory_event(event_id="same-event", content="same")
    same_reservations = await asyncio.gather(
        *(
            memory_store.reserve_event(user_id, session_id, event.event_id, event.sha256)
            for _ in range(100)
        )
    )
    assert {reservation.sequence for reservation in same_reservations} == {101}
    assert sum(reservation.state == "reserved" for reservation in same_reservations) == 1

    committed_event = event.model_copy(update={"sequence": 101})
    statuses = await asyncio.gather(
        *(
            memory_store.commit_event(user_id, session_id, committed_event)
            for _ in range(100)
        )
    )
    assert statuses.count("committed") == 1
    assert await memory_store.read_originals_after(user_id, session_id, 100) == (
        committed_event,
    )

import json

import pytest

from short_term_memory.jobs.redis_compression_queue import (
    CompressionJob,
    CompressionJobLease,
    RedisCompressionQueue,
)


class QueueRedis:
    """Faithful in-memory model of the durable queue's Lua state machine."""

    def __init__(self):
        self.values, self.lists = {}, {}
        self.sets, self.zsets = {}, {}

    async def get(self, key):
        return self.values.get(key)

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def _ready(self, key):
        return self.lists.setdefault(key, [])

    def _set(self, key):
        return self.sets.setdefault(key, set())

    def _zset(self, key):
        return self.zsets.setdefault(key, {})

    async def eval(self, script, numkeys, *args):
        keys, values = args[:numkeys], args[numkeys:]
        if "dream:compression:enqueue-v2" in script:
            (
                job_key, ready_key, ready_members_key, pending_key, pending_prefix,
                job_prefix,
            ) = keys
            payload, job_id, session_key, capacity = values
            existing = self.values.get(job_key)
            if existing is not None:
                if existing == payload:
                    return ["idempotent"]
                old, incoming = json.loads(existing), json.loads(payload)
                if (
                    old["job_id"] == incoming["job_id"]
                    and old["user_id"] == incoming["user_id"]
                    and old["session_id"] == incoming["session_id"]
                    and old["expected_version"] >= incoming["expected_version"]
                    and old["requested_through_sequence"]
                    >= incoming["requested_through_sequence"]
                    and (old.get("rebuild", False) or not incoming.get("rebuild", False))
                    and (
                        old.get("evict_oldest_generation", old.get("recompress", False))
                        or not incoming.get(
                            "evict_oldest_generation", incoming.get("recompress", False)
                        )
                    )
                ):
                    return ["idempotent"]
                return ["conflict"]
            self.values[job_key] = payload
            if len(self._ready(ready_key)) < int(capacity):
                self._ready(ready_key).append(job_id)
                self._set(ready_members_key).add(job_id)
                return ["ready"]
            pointer = f"{pending_prefix}{session_key}"
            previous = self.values.get(pointer)
            if previous and previous != job_id:
                previous_key = f"{job_prefix}{previous}"
                previous_payload = self.values.get(previous_key)
                if previous_payload is not None:
                    old, new = json.loads(previous_payload), json.loads(payload)
                    expected_version = max(
                        old["expected_version"], new["expected_version"]
                    )
                    through_sequence = max(
                        old["requested_through_sequence"],
                        new["requested_through_sequence"],
                    )
                    rebuild = old.get("rebuild", False) or new.get("rebuild", False)
                    evict_oldest_generation = old.get(
                        "evict_oldest_generation", old.get("recompress", False)
                    ) or new.get(
                        "evict_oldest_generation", new.get("recompress", False)
                    )
                    if (
                        expected_version == old["expected_version"]
                        and through_sequence == old["requested_through_sequence"]
                        and rebuild == old.get("rebuild", False)
                        and evict_oldest_generation
                        == old.get("evict_oldest_generation", old.get("recompress", False))
                    ):
                        self.values.pop(job_key, None)
                        return ["coalesced"]
                    new["expected_version"] = expected_version
                    new["requested_through_sequence"] = through_sequence
                    new["rebuild"] = rebuild
                    new["evict_oldest_generation"] = evict_oldest_generation
                    new.pop("recompress", None)
                    self.values[job_key] = json.dumps(new, separators=(",", ":"))
                self.values.pop(previous_key, None)
            self.values[pointer] = job_id
            self._set(pending_key).add(session_key)
            return ["pending"]
        if "dream:compression:lease-v2" in script:
            (
                ready_key, ready_members_key, inflight_key, retry_key, dead_key,
                corrupt_key, lease_prefix, job_prefix, pending_key, pending_prefix,
            ) = keys
            now = int(values[0])
            token = values[1]
            lease_ms = int(values[2])
            capacity = int(values[3])
            # Recover expired inflight entries before promotion/lease.
            for job_id, deadline in list(self._zset(inflight_key).items()):
                if deadline <= now:
                    del self._zset(inflight_key)[job_id]
                    self.values.pop(f"{lease_prefix}{job_id}", None)
                    if f"{job_prefix}{job_id}" in self.values:
                        self._ready(ready_key).append(job_id)
                        self._set(ready_members_key).add(job_id)
                    else:
                        self._zset(corrupt_key)[job_id] = now
            for job_id, due in list(self._zset(retry_key).items()):
                if due <= now:
                    del self._zset(retry_key)[job_id]
                    if job_id not in self._set(ready_members_key):
                        self._ready(ready_key).append(job_id)
                        self._set(ready_members_key).add(job_id)
            while len(self._ready(ready_key)) < capacity and self._set(pending_key):
                session = self._set(pending_key).pop()
                pointer = f"{pending_prefix}{session}"
                job_id = self.values.pop(pointer, None)
                if job_id is None:
                    continue
                if f"{job_prefix}{job_id}" not in self.values:
                    self._zset(corrupt_key)[job_id] = now
                    continue
                self._ready(ready_key).append(job_id)
                self._set(ready_members_key).add(job_id)
            while self._ready(ready_key):
                job_id = self._ready(ready_key).pop(0)
                self._set(ready_members_key).discard(job_id)
                payload = self.values.get(f"{job_prefix}{job_id}")
                if payload is None:
                    self._zset(corrupt_key)[job_id] = now
                    continue
                lease_key = f"{lease_prefix}{job_id}"
                if lease_key in self.values:
                    self._ready(ready_key).append(job_id)
                    self._set(ready_members_key).add(job_id)
                    return ["", ""]
                self.values[lease_key] = token
                self._zset(inflight_key)[job_id] = now + lease_ms
                return [job_id, payload]
            return ["", ""]
        if "dream:compression:ack-v2" in script:
            job_key, lease_key, inflight_key = keys
            if self.values.get(lease_key) != values[0]:
                return ["0"]
            self.values.pop(job_key, None)
            self.values.pop(lease_key, None)
            self._zset(inflight_key).pop(values[1], None)
            return ["1"]
        if "dream:compression:retry-v2" in script:
            job_key, lease_key, inflight_key, retry_key, dead_key = keys
            token, payload, job_id, attempt, due, max_attempts = values
            if self.values.get(lease_key) != token:
                return ["lost"]
            self.values.pop(lease_key, None)
            self._zset(inflight_key).pop(job_id, None)
            self.values[job_key] = payload
            target = dead_key if int(attempt) >= int(max_attempts) else retry_key
            self._zset(target)[job_id] = int(due)
            return ["dead" if target == dead_key else "retry"]
        if "dream:compression:return-lease-v1" in script:
            job_key, lease_key, inflight_key, retry_key = keys
            token, job_id, now = values
            if self.values.get(lease_key) != token:
                return ["lost"]
            self.values.pop(lease_key, None)
            self._zset(inflight_key).pop(job_id, None)
            if job_key not in self.values:
                return ["lost"]
            self._zset(retry_key)[job_id] = int(now)
            return ["returned"]
        raise AssertionError("unsupported queue script")


def compression_job(*, job_id="job-10-0", through_sequence=10, expected_version=0, session_id="s"):
    return CompressionJob(
        job_id=job_id, user_id="u", session_id=session_id,
        expected_version=expected_version, requested_through_sequence=through_sequence,
    )


def test_legacy_recompress_job_lazily_migrates_to_generation_eviction() -> None:
    legacy = compression_job().model_dump(mode="json")
    legacy.pop("evict_oldest_generation", None)
    legacy["recompress"] = True

    migrated = CompressionJob.model_validate(legacy)

    assert migrated.evict_oldest_generation is True
    assert "recompress" not in migrated.model_dump(mode="json")


def test_rebased_rebuild_preserves_scope_and_coverage_but_changes_identity() -> None:
    old = compression_job(
        job_id="old", through_sequence=180, expected_version=2
    ).model_copy(update={"rebuild": True})

    new = old.rebased(expected_version=3)

    assert new.user_id == old.user_id
    assert new.session_id == old.session_id
    assert new.expected_version == 3
    assert new.requested_through_sequence == 180
    assert new.rebuild is True
    assert new.job_id != old.job_id


@pytest.mark.asyncio
async def test_rebuild_intent_survives_queue_round_trip_and_wins_same_coverage():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, capacity=1)
    active = compression_job(job_id="active", session_id="active")
    normal = compression_job(job_id="normal", session_id="pending")
    rebuild = normal.model_copy(update={"job_id": "rebuild", "rebuild": True})
    await queue.enqueue(active)
    assert await queue.enqueue(normal) == "pending"
    assert await queue.enqueue(rebuild) == "pending"

    active_lease = await queue.lease("active-worker", now_unix_ms=0)
    assert active_lease is not None
    assert await queue.ack(active_lease)
    lease = await queue.lease("worker", now_unix_ms=1)

    assert lease is not None and lease.job.rebuild is True


@pytest.mark.asyncio
async def test_pending_merge_keeps_larger_normal_coverage_and_rebuild_intent():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, capacity=1)
    await queue.enqueue(compression_job(job_id="active", session_id="active"))
    rebuild = compression_job(job_id="rebuild", session_id="pending").model_copy(
        update={"rebuild": True}
    )
    larger_normal = compression_job(
        job_id="normal", session_id="pending", through_sequence=20
    )
    await queue.enqueue(rebuild)
    assert await queue.enqueue(larger_normal) == "pending"
    active = await queue.lease("active", now_unix_ms=0)
    assert active is not None
    await queue.ack(active)

    lease = await queue.lease("worker", now_unix_ms=1)

    assert lease is not None
    assert lease.job.requested_through_sequence == 20
    assert lease.job.rebuild is True


@pytest.mark.asyncio
async def test_pending_merge_keeps_rebuild_when_higher_version_normal_arrives():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, capacity=1)
    await queue.enqueue(compression_job(job_id="active", session_id="active"))
    rebuild = compression_job(
        job_id="rebuild", session_id="pending", through_sequence=100
    ).model_copy(update={"rebuild": True})
    newer_normal = compression_job(
        job_id="newer", session_id="pending", expected_version=1, through_sequence=10
    )
    await queue.enqueue(rebuild)
    assert await queue.enqueue(newer_normal) == "pending"
    active = await queue.lease("active", now_unix_ms=0)
    assert active is not None
    await queue.ack(active)

    lease = await queue.lease("worker", now_unix_ms=1)

    assert lease is not None
    assert lease.job.expected_version == 1
    assert lease.job.requested_through_sequence == 100
    assert lease.job.rebuild is True


@pytest.mark.asyncio
async def test_original_retry_of_a_merged_pending_job_is_idempotent_not_conflict():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, capacity=1)
    await queue.enqueue(compression_job(job_id="active", session_id="active"))
    rebuild = compression_job(job_id="rebuild", session_id="pending").model_copy(
        update={"rebuild": True}
    )
    normal = compression_job(job_id="normal", session_id="pending", through_sequence=20)
    await queue.enqueue(rebuild)
    await queue.enqueue(normal)

    assert await queue.enqueue(normal) == "idempotent"


@pytest.mark.asyncio
async def test_expired_inflight_lease_is_reclaimed_after_crash_or_cancellation():
    queue = RedisCompressionQueue(QueueRedis(), capacity=1, lease_seconds=1)
    job = compression_job()
    await queue.enqueue(job)
    abandoned = await queue.lease("worker-1", now_unix_ms=0)
    assert abandoned is not None

    reclaimed = await queue.lease("worker-2", now_unix_ms=1_001)

    assert reclaimed is not None and reclaimed.job == job
    assert reclaimed.token == "worker-2"
    assert await queue.ack(abandoned) is False
    assert await queue.ack(reclaimed) is True


@pytest.mark.asyncio
async def test_cancelled_worker_returns_owned_lease_for_immediate_reclaim():
    queue = RedisCompressionQueue(QueueRedis(), capacity=1, lease_seconds=300)
    job = compression_job()
    await queue.enqueue(job)
    abandoned = await queue.lease("worker-1", now_unix_ms=0)
    assert abandoned is not None

    assert await queue.return_lease(abandoned, now_unix_ms=1) == "returned"
    reclaimed = await queue.lease("worker-2", now_unix_ms=1)

    assert reclaimed is not None and reclaimed.job == job
    assert reclaimed.token == "worker-2"


@pytest.mark.asyncio
async def test_pending_session_is_promoted_and_newer_pending_job_coalesces_safely():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, capacity=1)
    active = compression_job(job_id="active", session_id="active")
    older = compression_job(job_id="older", session_id="pending")
    newest = compression_job(job_id="newest", session_id="pending", through_sequence=11)
    assert await queue.enqueue(active) == "ready"
    assert await queue.enqueue(older) == "pending"
    assert await queue.enqueue(newest) == "pending"

    active_lease = await queue.lease("worker-1", now_unix_ms=0)
    assert active_lease is not None
    assert await queue.ack(active_lease)
    promoted = await queue.lease("worker-2", now_unix_ms=1)

    assert promoted is not None and promoted.job.job_id == "newest"
    assert "dream:compression:job:older" not in redis.values


@pytest.mark.asyncio
async def test_pending_coalesce_keeps_highest_version_then_largest_coverage():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, capacity=1)
    active = compression_job(job_id="active", session_id="active")
    best = compression_job(
        job_id="best", session_id="pending", expected_version=1, through_sequence=100
    )
    late_old = compression_job(
        job_id="late-old", session_id="pending", expected_version=0, through_sequence=50
    )
    late_short = compression_job(
        job_id="late-short", session_id="pending", expected_version=1, through_sequence=50
    )
    await queue.enqueue(active)
    assert await queue.enqueue(best) == "pending"
    assert await queue.enqueue(late_old) == "coalesced"
    assert await queue.enqueue(late_short) == "coalesced"
    assert redis.sets[queue.PENDING_KEY] == {queue._session_key(best)}

    active_lease = await queue.lease("active-worker", now_unix_ms=0)
    assert active_lease is not None
    assert await queue.ack(active_lease)
    promoted = await queue.lease("pending-worker", now_unix_ms=1)

    assert promoted is not None and promoted.job == best
    assert "dream:compression:job:late-old" not in redis.values
    assert "dream:compression:job:late-short" not in redis.values


@pytest.mark.asyncio
async def test_missing_payload_is_quarantined_and_next_ready_job_is_leased():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, capacity=2)
    missing, valid = compression_job(job_id="missing"), compression_job(job_id="valid")
    await queue.enqueue(missing)
    await queue.enqueue(valid)
    redis.values.pop("dream:compression:job:missing")

    lease = await queue.lease("worker", now_unix_ms=0)

    assert lease is not None and lease.job == valid
    assert await queue.corrupt_job_count() == 1
    assert "dream:compression:lease:missing" not in redis.values


@pytest.mark.asyncio
async def test_duplicate_id_is_idempotent_but_never_overwrites_live_payload():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis)
    job = compression_job(job_id="unique")
    assert await queue.enqueue(job) == "ready"
    assert await queue.enqueue(job) == "idempotent"
    conflicting = job.model_copy(update={"requested_through_sequence": 99})

    with pytest.raises(ValueError, match="conflicts"):
        await queue.enqueue(conflicting)
    lease = await queue.lease("worker", now_unix_ms=0)
    assert lease is not None and lease.job == job
    assert redis.lists[queue.READY_KEY] == []


@pytest.mark.asyncio
async def test_lost_ownership_is_reported_by_ack_and_retry():
    queue = RedisCompressionQueue(QueueRedis())
    await queue.enqueue(compression_job())
    lease = await queue.lease("owner", now_unix_ms=0)
    assert lease is not None
    lost = CompressionJobLease(lease.job, "not-owner")

    assert await queue.ack(lost) is False
    assert await queue.retry(lost, now_unix_ms=0) == "lost"


@pytest.mark.asyncio
async def test_dead_letter_retains_payload_and_exposes_its_count():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, max_attempts=1)
    job = compression_job()
    await queue.enqueue(job)
    lease = await queue.lease("owner", now_unix_ms=0)
    assert lease is not None

    assert await queue.retry(lease, now_unix_ms=0) == "dead"
    assert await queue.dead_letter_count() == 1
    assert json.loads(redis.values["dream:compression:job:job-10-0"])["job_id"] == job.job_id

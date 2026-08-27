import pytest

from short_term_memory.jobs.redis_compression_queue import (
    ACK_SCRIPT,
    ENQUEUE_SCRIPT,
    LEASE_SCRIPT,
    RETRY_SCRIPT,
)
from short_term_memory.jobs.session_memory_queue import (
    RedisSessionMemoryQueue,
    SessionMemoryJob,
)


class QueueRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.zsets: dict[str, dict[str, int]] = {}

    def ready(self, key: str) -> list[str]:
        return self.lists.setdefault(key, [])

    def members(self, key: str) -> set[str]:
        return self.sets.setdefault(key, set())

    def scored(self, key: str) -> dict[str, int]:
        return self.zsets.setdefault(key, {})

    async def zcard(self, key: str) -> int:
        return len(self.scored(key))

    async def eval(self, script: str, numkeys: int, *args: str) -> list[str]:
        keys, values = args[:numkeys], args[numkeys:]
        if script == ENQUEUE_SCRIPT:
            job_key, ready_key, ready_members, pending, pending_prefix, _job_prefix = keys
            payload, job_id, session_key, capacity = values
            existing = self.values.get(job_key)
            if existing is not None:
                return ["idempotent" if existing == payload else "conflict"]
            self.values[job_key] = payload
            if len(self.ready(ready_key)) < int(capacity):
                self.ready(ready_key).append(job_id)
                self.members(ready_members).add(job_id)
                return ["ready"]
            self.values[f"{pending_prefix}{session_key}"] = job_id
            self.members(pending).add(session_key)
            return ["pending"]
        if script == LEASE_SCRIPT:
            (
                ready_key, ready_members, inflight, retry, _dead, corrupt,
                lease_prefix, job_prefix, _pending, _pending_prefix,
            ) = keys
            now, token, lease_ms, _capacity = values
            for job_id, deadline in list(self.scored(inflight).items()):
                if deadline <= int(now):
                    self.scored(inflight).pop(job_id)
                    self.values.pop(f"{lease_prefix}{job_id}", None)
                    if f"{job_prefix}{job_id}" in self.values:
                        self.ready(ready_key).append(job_id)
                        self.members(ready_members).add(job_id)
                    else:
                        self.scored(corrupt)[job_id] = int(now)
            for job_id, due in list(self.scored(retry).items()):
                if due <= int(now):
                    self.scored(retry).pop(job_id)
                    self.ready(ready_key).append(job_id)
                    self.members(ready_members).add(job_id)
            if not self.ready(ready_key):
                return ["", ""]
            job_id = self.ready(ready_key).pop(0)
            self.members(ready_members).discard(job_id)
            payload = self.values[f"{job_prefix}{job_id}"]
            self.values[f"{lease_prefix}{job_id}"] = token
            self.scored(inflight)[job_id] = int(now) + int(lease_ms)
            return [job_id, payload]
        if script == ACK_SCRIPT:
            job_key, lease_key, inflight = keys
            token, job_id = values
            if self.values.get(lease_key) != token:
                return ["0"]
            self.values.pop(job_key, None)
            self.values.pop(lease_key, None)
            self.scored(inflight).pop(job_id, None)
            return ["1"]
        if script == RETRY_SCRIPT:
            job_key, lease_key, inflight, retry, dead = keys
            token, payload, job_id, attempt, due, max_attempts = values
            if self.values.get(lease_key) != token:
                return ["lost"]
            self.values.pop(lease_key, None)
            self.scored(inflight).pop(job_id, None)
            self.values[job_key] = payload
            target = dead if int(attempt) >= int(max_attempts) else retry
            self.scored(target)[job_id] = int(due)
            return ["dead" if target == dead else "retry"]
        raise AssertionError("unsupported queue script")


def job(*, version: int = 2, through: int = 12) -> SessionMemoryJob:
    return SessionMemoryJob(
        user_id="u", session_id="s", expected_version=version,
        requested_through_sequence=through,
    )


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_for_extraction_identity() -> None:
    queue = RedisSessionMemoryQueue(QueueRedis())
    assert await queue.enqueue(job()) == "ready"
    assert await queue.enqueue(job()) == "idempotent"
    assert job().job_id == job().job_id
    assert job(through=13).job_id != job().job_id


@pytest.mark.asyncio
async def test_sixty_second_lease_is_recovered_after_deadline() -> None:
    queue = RedisSessionMemoryQueue(QueueRedis(), lease_seconds=60)
    await queue.enqueue(job())
    first = await queue.lease("worker-one", now_unix_ms=1_000)
    assert first is not None
    assert await queue.lease("worker-two", now_unix_ms=60_999) is None
    recovered = await queue.lease("worker-two", now_unix_ms=61_000)
    assert recovered is not None
    assert recovered.job == first.job
    assert recovered.token == "worker-two"

"""Durable Redis state machine for deferred Headroom compression jobs."""

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from short_term_memory.storage.vfs_adapter import safe_component


class AsyncRedisQueueClient(Protocol):
    async def eval(self, script: str, numkeys: int, *args: str) -> Any: ...

    async def zcard(self, key: str) -> int: ...


class CompressionJob(BaseModel):
    """Persistent compression intent. Conversation content never enters a job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    requested_through_sequence: int = Field(ge=1)
    attempt: int = Field(ge=0, default=0)
    rebuild: bool = False
    evict_oldest_generation: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_recompress(cls, value: Any) -> Any:
        """Accept jobs queued before the eviction intent was named accurately."""

        if not isinstance(value, Mapping) or "recompress" not in value:
            return value
        migrated = dict(value)
        legacy = migrated.pop("recompress")
        if "evict_oldest_generation" not in migrated or legacy is True:
            migrated["evict_oldest_generation"] = legacy
        return migrated

    def rebased(self, *, expected_version: int) -> "CompressionJob":
        """Retarget a durable full rebuild after an envelope-version race."""

        if not self.rebuild:
            raise ValueError("only rebuild jobs can be rebased")
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        identity = (
            f"{self.user_id}\n{self.session_id}\n{expected_version}\n"
            f"{self.requested_through_sequence}\nTrue\n"
            f"{self.evict_oldest_generation}"
        )
        return self.model_copy(
            update={
                "job_id": f"memory-{uuid5(NAMESPACE_URL, identity).hex}",
                "expected_version": expected_version,
            }
        )


@dataclass(frozen=True)
class CompressionJobLease:
    job: CompressionJob
    token: str


ENQUEUE_SCRIPT = """
-- dream:compression:enqueue-v2
local function evicts_oldest(job)
  return job.evict_oldest_generation == true or job.recompress == true
end
local existing = redis.call('GET', KEYS[1])
if existing then
  if existing == ARGV[1] then return {'idempotent'} end
  local old = cjson.decode(existing)
  local incoming = cjson.decode(ARGV[1])
  if old.job_id == incoming.job_id and old.user_id == incoming.user_id
    and old.session_id == incoming.session_id
    and old.expected_version >= incoming.expected_version
    and old.requested_through_sequence >= incoming.requested_through_sequence
    and ((old.rebuild == true) or (incoming.rebuild ~= true))
    and (evicts_oldest(old) or not evicts_oldest(incoming)) then
    return {'idempotent'}
  end
  return {'conflict'}
end
redis.call('SET', KEYS[1], ARGV[1])
if redis.call('LLEN', KEYS[2]) < tonumber(ARGV[4]) then
  redis.call('RPUSH', KEYS[2], ARGV[2])
  redis.call('SADD', KEYS[3], ARGV[2])
  return {'ready'}
end
local pointer = KEYS[5] .. ARGV[3]
local previous = redis.call('GET', pointer)
if previous and previous ~= ARGV[2] then
  local previous_payload = redis.call('GET', KEYS[6] .. previous)
  if previous_payload then
    local old = cjson.decode(previous_payload)
    local new = cjson.decode(ARGV[1])
    local expected_version = math.max(old.expected_version, new.expected_version)
    local through_sequence = math.max(
      old.requested_through_sequence, new.requested_through_sequence)
    local rebuild = (old.rebuild == true) or (new.rebuild == true)
    local evict_oldest_generation = evicts_oldest(old) or evicts_oldest(new)
    if expected_version == old.expected_version
      and through_sequence == old.requested_through_sequence
      and rebuild == (old.rebuild == true)
      and evict_oldest_generation == evicts_oldest(old) then
      redis.call('DEL', KEYS[1])
      return {'coalesced'}
    end
    new.expected_version = expected_version
    new.requested_through_sequence = through_sequence
    new.rebuild = rebuild
    new.evict_oldest_generation = evict_oldest_generation
    new.recompress = nil
    redis.call('SET', KEYS[1], cjson.encode(new))
  end
  redis.call('DEL', KEYS[6] .. previous)
end
redis.call('SET', pointer, ARGV[2])
redis.call('SADD', KEYS[4], ARGV[3])
return {'pending'}
"""

LEASE_SCRIPT = """
-- dream:compression:lease-v2
local function publish(job_id)
  if redis.call('SISMEMBER', KEYS[2], job_id) == 1 then return end
  if redis.call('ZSCORE', KEYS[3], job_id) then return end
  if redis.call('ZSCORE', KEYS[4], job_id) then return end
  redis.call('RPUSH', KEYS[1], job_id)
  redis.call('SADD', KEYS[2], job_id)
end
for _, job_id in ipairs(redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', ARGV[1])) do
  if redis.call('ZREM', KEYS[3], job_id) == 1 then
    redis.call('DEL', KEYS[7] .. job_id)
    if redis.call('GET', KEYS[8] .. job_id) then
      publish(job_id)
    else
      redis.call('ZADD', KEYS[6], ARGV[1], job_id)
    end
  end
end
for _, job_id in ipairs(redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', ARGV[1])) do
  if redis.call('ZREM', KEYS[4], job_id) == 1 then publish(job_id) end
end
while redis.call('LLEN', KEYS[1]) < tonumber(ARGV[4]) do
  local session = redis.call('SPOP', KEYS[9])
  if not session then break end
  local pointer = KEYS[10] .. session
  local job_id = redis.call('GET', pointer)
  redis.call('DEL', pointer)
  if job_id then
    if redis.call('GET', KEYS[8] .. job_id) then
      publish(job_id)
    else
      redis.call('ZADD', KEYS[6], ARGV[1], job_id)
    end
  end
end
while true do
  local job_id = redis.call('LPOP', KEYS[1])
  if not job_id then return {'', ''} end
  redis.call('SREM', KEYS[2], job_id)
  local payload = redis.call('GET', KEYS[8] .. job_id)
  if not payload then
    redis.call('ZADD', KEYS[6], ARGV[1], job_id)
  else
    local lease_key = KEYS[7] .. job_id
    if redis.call('SET', lease_key, ARGV[2], 'NX', 'PX', ARGV[3]) then
      redis.call('ZADD', KEYS[3], tonumber(ARGV[1]) + tonumber(ARGV[3]), job_id)
      return {job_id, payload}
    end
    publish(job_id)
    return {'', ''}
  end
end
"""

ACK_SCRIPT = """
-- dream:compression:ack-v2
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return {'0'} end
redis.call('DEL', KEYS[1], KEYS[2])
redis.call('ZREM', KEYS[3], ARGV[2])
return {'1'}
"""

RETRY_SCRIPT = """
-- dream:compression:retry-v2
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return {'lost'} end
redis.call('DEL', KEYS[2])
redis.call('ZREM', KEYS[3], ARGV[3])
redis.call('SET', KEYS[1], ARGV[2])
if tonumber(ARGV[4]) >= tonumber(ARGV[6]) then
  redis.call('ZADD', KEYS[5], ARGV[5], ARGV[3])
  return {'dead'}
end
redis.call('ZADD', KEYS[4], ARGV[5], ARGV[3])
return {'retry'}
"""

RETURN_LEASE_SCRIPT = """
-- dream:compression:return-lease-v1
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return {'lost'} end
redis.call('DEL', KEYS[2])
redis.call('ZREM', KEYS[3], ARGV[2])
if not redis.call('GET', KEYS[1]) then return {'lost'} end
redis.call('ZADD', KEYS[4], ARGV[3], ARGV[2])
return {'returned'}
"""


class RedisCompressionQueue:
    READY_KEY = "dream:compression:ready"
    READY_MEMBERS_KEY = "dream:compression:ready-members"
    INFLIGHT_KEY = "dream:compression:inflight"
    RETRY_KEY = "dream:compression:retry"
    PENDING_KEY = "dream:compression:pending"
    DEAD_KEY = "dream:compression:dead"
    CORRUPT_KEY = "dream:compression:corrupt"
    JOB_PREFIX = "dream:compression:job:"
    LEASE_PREFIX = "dream:compression:lease:"
    PENDING_PREFIX = "dream:compression:pending-job:"

    def __init__(
        self,
        client: AsyncRedisQueueClient,
        *,
        capacity: int = 10_000,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        initial_backoff_seconds: int = 1,
        max_backoff_seconds: int = 300,
    ) -> None:
        if min(capacity, lease_seconds, max_attempts, initial_backoff_seconds, max_backoff_seconds) < 1:
            raise ValueError("queue limits must be positive")
        self.client = client
        self.capacity = capacity
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.initial_backoff_seconds = initial_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds

    async def enqueue(self, job: CompressionJob) -> str:
        job_key = self._job_key(job.job_id)
        result = await self.client.eval(
            ENQUEUE_SCRIPT,
            6,
            job_key,
            self.READY_KEY,
            self.READY_MEMBERS_KEY,
            self.PENDING_KEY,
            self.PENDING_PREFIX,
            self.JOB_PREFIX,
            job.model_dump_json(),
            self._job_component(job.job_id),
            self._session_key(job),
            str(self.capacity),
        )
        state = self._text(result[0])
        if state == "conflict":
            raise ValueError("job_id conflicts with a durable payload")
        return state

    async def lease(
        self, worker_token: str, *, now_unix_ms: int
    ) -> CompressionJobLease | None:
        if not worker_token:
            raise ValueError("worker_token must not be blank")
        result = await self.client.eval(
            LEASE_SCRIPT,
            10,
            self.READY_KEY,
            self.READY_MEMBERS_KEY,
            self.INFLIGHT_KEY,
            self.RETRY_KEY,
            self.DEAD_KEY,
            self.CORRUPT_KEY,
            self.LEASE_PREFIX,
            self.JOB_PREFIX,
            self.PENDING_KEY,
            self.PENDING_PREFIX,
            str(now_unix_ms),
            worker_token,
            str(self.lease_seconds * 1000),
            str(self.capacity),
        )
        job_id, payload = (self._text(value) for value in result[:2])
        if not job_id:
            return None
        job = CompressionJob.model_validate_json(payload)
        if self._job_component(job.job_id) != job_id:
            raise ValueError("durable compression job ID is invalid")
        return CompressionJobLease(job=job, token=worker_token)

    async def ack(self, lease: CompressionJobLease) -> bool:
        result = await self.client.eval(
            ACK_SCRIPT,
            3,
            self._job_key(lease.job.job_id),
            self._lease_key(lease.job.job_id),
            self.INFLIGHT_KEY,
            lease.token,
            self._job_component(lease.job.job_id),
        )
        return self._text(result[0]) == "1"

    async def retry(self, lease: CompressionJobLease, *, now_unix_ms: int) -> str:
        job = lease.job.model_copy(update={"attempt": lease.job.attempt + 1})
        backoff_seconds = min(
            self.max_backoff_seconds,
            self.initial_backoff_seconds * (2 ** (job.attempt - 1)),
        )
        result = await self.client.eval(
            RETRY_SCRIPT,
            5,
            self._job_key(job.job_id),
            self._lease_key(job.job_id),
            self.INFLIGHT_KEY,
            self.RETRY_KEY,
            self.DEAD_KEY,
            lease.token,
            job.model_dump_json(),
            self._job_component(job.job_id),
            str(job.attempt),
            str(now_unix_ms + backoff_seconds * 1_000),
            str(self.max_attempts),
        )
        return self._text(result[0])

    async def return_lease(
        self, lease: CompressionJobLease, *, now_unix_ms: int
    ) -> str:
        """Return a cancelled owned lease without consuming a retry attempt."""

        result = await self.client.eval(
            RETURN_LEASE_SCRIPT,
            4,
            self._job_key(lease.job.job_id),
            self._lease_key(lease.job.job_id),
            self.INFLIGHT_KEY,
            self.RETRY_KEY,
            lease.token,
            self._job_component(lease.job.job_id),
            str(now_unix_ms),
        )
        return self._text(result[0])

    async def dead_letter_count(self) -> int:
        return int(await self.client.zcard(self.DEAD_KEY))

    async def corrupt_job_count(self) -> int:
        return int(await self.client.zcard(self.CORRUPT_KEY))

    @classmethod
    def _job_key(cls, job_id: str) -> str:
        return f"{cls.JOB_PREFIX}{cls._job_component(job_id)}"

    @classmethod
    def _lease_key(cls, job_id: str) -> str:
        return f"{cls.LEASE_PREFIX}{cls._job_component(job_id)}"

    @staticmethod
    def _job_component(job_id: str) -> str:
        safe_component(job_id, "job_id")
        if ":" in job_id:
            raise ValueError("job_id must not contain ':'")
        return job_id

    @classmethod
    def _session_key(cls, job: CompressionJob) -> str:
        user = cls._component(job.user_id, "user_id")
        session = cls._component(job.session_id, "session_id")
        return f"{len(user)}:{user}:{len(session)}:{session}"

    @staticmethod
    def _component(value: str, label: str) -> str:
        safe_component(value, label)
        if ":" in value:
            raise ValueError(f"{label} must not contain ':'")
        return value

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

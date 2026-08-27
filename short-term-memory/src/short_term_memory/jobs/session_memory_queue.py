"""Durable, namespaced queue for isolated Session Memory updates."""

from dataclasses import dataclass
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from short_term_memory.jobs.redis_compression_queue import (
    CompressionJob,
    CompressionJobLease,
    RedisCompressionQueue,
)


class SessionMemoryJob(BaseModel):
    """An extraction intent identified by session, envelope version and coverage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    requested_through_sequence: int = Field(ge=1)
    attempt: int = Field(default=0, ge=0)

    @property
    def job_id(self) -> str:
        identity = (
            f"{len(self.user_id)}:{self.user_id}:"
            f"{len(self.session_id)}:{self.session_id}:"
            f"{self.expected_version}:{self.requested_through_sequence}"
        )
        return f"session-memory-{sha256(identity.encode()).hexdigest()[:32]}"


@dataclass(frozen=True)
class SessionMemoryJobLease:
    job: SessionMemoryJob
    token: str


class _NamespacedQueue(RedisCompressionQueue):
    READY_KEY = "dream:session-memory:ready"
    READY_MEMBERS_KEY = "dream:session-memory:ready-members"
    INFLIGHT_KEY = "dream:session-memory:inflight"
    RETRY_KEY = "dream:session-memory:retry"
    PENDING_KEY = "dream:session-memory:pending"
    DEAD_KEY = "dream:session-memory:dead"
    CORRUPT_KEY = "dream:session-memory:corrupt"
    JOB_PREFIX = "dream:session-memory:job:"
    LEASE_PREFIX = "dream:session-memory:lease:"
    PENDING_PREFIX = "dream:session-memory:pending-job:"


class RedisSessionMemoryQueue:
    """Typed Session Memory facade over the proven Redis queue state machine."""

    def __init__(self, client: object, **kwargs: int) -> None:
        self._queue = _NamespacedQueue(client, **kwargs)

    @staticmethod
    def _compression_job(job: SessionMemoryJob) -> CompressionJob:
        return CompressionJob(
            job_id=job.job_id,
            user_id=job.user_id,
            session_id=job.session_id,
            expected_version=job.expected_version,
            requested_through_sequence=job.requested_through_sequence,
            attempt=job.attempt,
        )

    @staticmethod
    def _session_job(job: CompressionJob) -> SessionMemoryJob:
        return SessionMemoryJob(
            user_id=job.user_id,
            session_id=job.session_id,
            expected_version=job.expected_version,
            requested_through_sequence=job.requested_through_sequence,
            attempt=job.attempt,
        )

    async def enqueue(self, job: SessionMemoryJob) -> str:
        return await self._queue.enqueue(self._compression_job(job))

    async def lease(
        self, worker_token: str, *, now_unix_ms: int
    ) -> SessionMemoryJobLease | None:
        lease = await self._queue.lease(worker_token, now_unix_ms=now_unix_ms)
        if lease is None:
            return None
        return SessionMemoryJobLease(
            job=self._session_job(lease.job), token=lease.token
        )

    async def ack(self, lease: SessionMemoryJobLease) -> bool:
        return await self._queue.ack(
            CompressionJobLease(
                job=self._compression_job(lease.job), token=lease.token
            )
        )

    async def retry(
        self, lease: SessionMemoryJobLease, *, now_unix_ms: int
    ) -> str:
        return await self._queue.retry(
            CompressionJobLease(
                job=self._compression_job(lease.job), token=lease.token
            ),
            now_unix_ms=now_unix_ms,
        )

    async def return_lease(
        self, lease: SessionMemoryJobLease, *, now_unix_ms: int
    ) -> str:
        return await self._queue.return_lease(
            CompressionJobLease(
                job=self._compression_job(lease.job), token=lease.token
            ),
            now_unix_ms=now_unix_ms,
        )

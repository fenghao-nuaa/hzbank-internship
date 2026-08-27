"""Cross-process completion boundary for cold Headroom rebuilds."""

import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Callable, Protocol

from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.jobs.redis_compression_queue import CompressionJob
from short_term_memory.models import MemorySummaryEnvelope


class CompletionRedisClient(Protocol):
    async def set(self, key: str, value: str, *, ex: int) -> Any: ...

    async def get(self, key: str) -> Any | None: ...


class EnvelopeReader(Protocol):
    async def read_envelope(
        self, user_id: str, session_id: str
    ) -> MemorySummaryEnvelope | None: ...


class RedisRebuildCompletion:
    """Publish content-free markers and wait for a fresh session envelope.

    The durable marker closes the publish-before-wait race.  The envelope is the
    source of truth, so coalesced jobs and a lost marker are both safe.
    """

    KEY_PREFIX = "dream:rebuild-completion:"

    def __init__(
        self,
        client: CompletionRedisClient,
        *,
        store: EnvelopeReader,
        scope_factory: OptimizationScopeFactory,
        ttl_seconds: int = 300,
        poll_seconds: float = 0.05,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.client = client
        self.store = store
        self.scope_factory = scope_factory
        self.ttl_seconds = ttl_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def publish(
        self, job: CompressionJob, envelope: MemorySummaryEnvelope
    ) -> None:
        """Persist a bounded marker after the worker's CAS and ACK complete."""

        if not self._matches(job, envelope):
            raise ValueError("completion envelope does not satisfy rebuild target")
        payload = json.dumps(
            {
                "version": envelope.version,
                "through_sequence": envelope.compressed_through_sequence,
            },
            separators=(",", ":"),
        )
        await self.client.set(self._key(job), payload, ex=self.ttl_seconds)

    async def wait_for(
        self, job: CompressionJob, timeout_seconds: float
    ) -> MemorySummaryEnvelope | None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    envelope = await self.store.read_envelope(
                        job.user_id, job.session_id
                    )
                    if self._matches(job, envelope):
                        return envelope

                    # The marker is an optimization, while the envelope remains
                    # authoritative. Notification-channel failure therefore
                    # cannot prevent bounded envelope polling.
                    try:
                        await self.client.get(self._key(job))
                    except Exception:
                        pass
                    await asyncio.sleep(self.poll_seconds)
        except TimeoutError:
            return None

    def _key(self, job: CompressionJob) -> str:
        scope = self.scope_factory.for_session(job.user_id, job.session_id)
        return f"{self.KEY_PREFIX}{scope.session_scope}"

    def _matches(
        self, job: CompressionJob, envelope: MemorySummaryEnvelope | None
    ) -> bool:
        if (
            envelope is None
            or envelope.version <= job.expected_version
            or envelope.compressed_through_sequence
            < job.requested_through_sequence
        ):
            return False
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("completion clock must be timezone-aware")
        return any(
            generation.from_sequence <= 1
            and generation.through_sequence >= job.requested_through_sequence
            and self._expires_at(generation.ccr_expires_at) > now
            for generation in envelope.compression_generations
        )

    @staticmethod
    def _expires_at(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("CCR timestamps must be timezone-aware")
        return parsed

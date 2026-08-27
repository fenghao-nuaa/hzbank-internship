"""Distributed worker for Claude-style background Session Memory updates."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid
from typing import Callable

import anyio

from short_term_memory.compression.continuity_model import ContinuityCompactionModel
from short_term_memory.compression.session_memory import extract_session_memory_revision
from short_term_memory.compression.session_memory_prompt import EMPTY_SESSION_MEMORY
from short_term_memory.jobs.session_memory_queue import (
    RedisSessionMemoryQueue,
    SessionMemoryJobLease,
)
from short_term_memory.models import MemoryEvent, SessionCompressionMessage
from short_term_memory.ports import AsyncMemoryStore
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.compaction_checkpoint import checkpoint_from_envelope


@dataclass(frozen=True)
class SessionMemoryWorkerResult:
    state: str
    job_id: str | None = None


def _has_tool_calls(event: MemoryEvent) -> bool:
    return event.metadata.get("has_tool_calls", "").lower() in {"1", "true", "yes"}


def last_safe_complete_round_sequence(events: tuple[MemoryEvent, ...]) -> int | None:
    """Claude advances coverage only through an assistant turn without tool use."""

    return next(
        (
            event.sequence
            for event in reversed(events)
            if event.role.value == "assistant" and not _has_tool_calls(event)
        ),
        None,
    )


class SessionMemoryWorker:
    def __init__(
        self,
        *,
        queue: RedisSessionMemoryQueue,
        store: AsyncMemoryStore,
        journals: JournalStore,
        continuity_model: ContinuityCompactionModel,
        model_name: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not model_name:
            raise ValueError("model_name must not be blank")
        self.queue = queue
        self.store = store
        self.journals = journals
        self.continuity_model = continuity_model
        self.model_name = model_name
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run_once(self) -> SessionMemoryWorkerResult:
        now = self._now()
        lease = await self.queue.lease(
            uuid.uuid4().hex, now_unix_ms=self._unix_ms(now)
        )
        if lease is None:
            return SessionMemoryWorkerResult("idle")
        extraction_token = uuid.uuid4().hex
        acquired = False
        try:
            acquired = await self.store.acquire_session_memory_extraction(
                lease.job.user_id,
                lease.job.session_id,
                extraction_token,
                expected_version=lease.job.expected_version,
                started_at=now.isoformat(),
            )
            if not acquired:
                return await self._retry(lease, "deferred")
            return await self._execute(lease, now)
        except asyncio.CancelledError:
            if hasattr(self.queue, "return_lease"):
                await asyncio.shield(
                    self.queue.return_lease(
                        lease, now_unix_ms=self._unix_ms(self._now())
                    )
                )
            raise
        except Exception:
            return await self._retry(lease, "retry")
        finally:
            if acquired:
                await asyncio.shield(
                    self.store.release_session_memory_extraction(
                        lease.job.user_id,
                        lease.job.session_id,
                        extraction_token,
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
        while not stopping.is_set():
            result = await self.run_once()
            if result.state == "idle":
                try:
                    async with asyncio.timeout(poll_seconds):
                        await stopping.wait()
                except TimeoutError:
                    pass

    async def _execute(
        self, lease: SessionMemoryJobLease, started_at: datetime
    ) -> SessionMemoryWorkerResult:
        job = lease.job
        envelope = await self.store.read_envelope(job.user_id, job.session_id)
        current_version = envelope.version if envelope is not None else 0
        if envelope is None or current_version != job.expected_version:
            return await self._ack(lease, "stale")

        events = self.journals.read_original_range(
            job.user_id, job.session_id, 1, job.requested_through_sequence
        )
        safe_sequence = last_safe_complete_round_sequence(events)
        if safe_sequence is None:
            return await self._ack(lease, "stale")
        messages = tuple(
            SessionCompressionMessage(
                role=event.role.value,
                content=event.content,
            )
            for event in events
        )
        previous = envelope.session_memory
        revision = await extract_session_memory_revision(
            current_memory=(previous.content if previous else EMPTY_SESSION_MEMORY),
            messages=messages,
            covered_through_sequence=safe_sequence,
            previous_version=(previous.version if previous else 0),
            continuity_model=self.continuity_model,
            model_name=self.model_name,
            extraction_started_at=started_at,
            now=self._now(),
        )
        completed_at = self._now()
        next_envelope = envelope.model_copy(
            update={
                "version": envelope.version + 1,
                "session_memory": revision,
                "updated_at": completed_at.isoformat(),
            }
        )
        written = await self.store.compare_and_set_envelope(
            job.user_id,
            job.session_id,
            job.expected_version,
            next_envelope,
        )
        if not written:
            return await self._ack(lease, "stale")
        checkpoint = checkpoint_from_envelope(
            job.user_id, job.session_id, next_envelope
        )
        await anyio.to_thread.run_sync(
            self.journals.append_compaction_checkpoint,
            job.user_id,
            job.session_id,
            checkpoint,
        )
        return await self._ack(lease, "acked")

    async def _ack(
        self, lease: SessionMemoryJobLease, state: str
    ) -> SessionMemoryWorkerResult:
        acked = await self.queue.ack(lease)
        return SessionMemoryWorkerResult(
            state=state if acked else "lost", job_id=lease.job.job_id
        )

    async def _retry(
        self, lease: SessionMemoryJobLease, state: str
    ) -> SessionMemoryWorkerResult:
        retry_state = await self.queue.retry(
            lease, now_unix_ms=self._unix_ms(self._now())
        )
        return SessionMemoryWorkerResult(
            state=state if retry_state == "retry" else retry_state,
            job_id=lease.job.job_id,
        )

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("worker clock must return a timezone-aware datetime")
        return now

    @staticmethod
    def _unix_ms(value: datetime) -> int:
        return int(value.timestamp() * 1_000)

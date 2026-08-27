"""Cold activation of bounded historical-session context before new writes."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

import anyio

from short_term_memory.compression.session_memory_compact import (
    materialize_session_memory_recovery_revision,
)
from short_term_memory.jobs.redis_compression_queue import CompressionJob
from short_term_memory.models import MemoryEvent, MemorySummaryEnvelope
from short_term_memory.ports import AsyncMemoryStore
from short_term_memory.storage.compaction_checkpoint import (
    CompactionCheckpoint,
    checkpoint_from_envelope,
    checkpoint_to_envelope,
)
from short_term_memory.storage.journal_store import JournalStore


class CompressionQueue(Protocol):
    async def enqueue(self, job: CompressionJob) -> str: ...


class SessionActivationUnavailableError(RuntimeError):
    """Raised when another cold activation does not finish within the bound."""


@dataclass(frozen=True)
class SessionActivationResult:
    recovered: bool
    latest_sequence: int
    checkpoint_id: str | None
    rebuild_queued: bool


class SessionActivator:
    """Restore Journal continuity state into an empty Redis session projection."""

    def __init__(
        self,
        *,
        store: AsyncMemoryStore,
        journals: JournalStore,
        compression_queue: CompressionQueue,
        history_turns: int,
        activation_timeout_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if history_turns < 1:
            raise ValueError("history_turns must be positive")
        if activation_timeout_seconds <= 0:
            raise ValueError("activation_timeout_seconds must be positive")
        self.store = store
        self.journals = journals
        self.compression_queue = compression_queue
        self.history_turns = history_turns
        self.activation_timeout_seconds = activation_timeout_seconds
        self.clock = clock

    async def activate(
        self,
        user_id: str,
        session_id: str,
        history_turns: int | None = None,
    ) -> SessionActivationResult:
        turns = self.history_turns if history_turns is None else history_turns
        if turns < 1:
            raise ValueError("history_turns must be positive")
        if await self.store.read_latest_sequence(user_id, session_id) > 0:
            return await self._warm_result(user_id, session_id)

        token = uuid4().hex
        acquired = await self.store.acquire_session_activation_lease(
            user_id, session_id, token
        )
        if not acquired:
            return await self._wait_for_projection(user_id, session_id)
        try:
            if await self.store.read_latest_sequence(user_id, session_id) > 0:
                return await self._warm_result(user_id, session_id)
            checkpoint, latest, recent = await self._read_journal(
                user_id, session_id, turns
            )
            if latest == 0:
                return SessionActivationResult(False, 0, None, False)
            if checkpoint is not None and checkpoint.compressed_through_sequence > latest:
                checkpoint = None
            restored_envelope = self._restore_envelope(checkpoint, recent)
            restored = await self.store.restore_session_projection(
                user_id,
                session_id,
                latest_sequence=latest,
                originals=recent,
                envelope=restored_envelope,
            )
            if not restored:
                return await self._warm_result(user_id, session_id)
            await self.compression_queue.enqueue(
                self._rebuild_job(user_id, session_id, latest, restored_envelope)
            )
            return SessionActivationResult(
                recovered=True,
                latest_sequence=latest,
                checkpoint_id=(checkpoint.checkpoint_id if checkpoint else None),
                rebuild_queued=True,
            )
        finally:
            await self.store.release_session_activation_lease(
                user_id, session_id, token
            )

    async def _warm_result(
        self, user_id: str, session_id: str
    ) -> SessionActivationResult:
        latest = await self.store.read_latest_sequence(user_id, session_id)
        envelope = await self.store.read_envelope(user_id, session_id)
        checkpoint_id = None
        if envelope is not None and (
            envelope.active_revision is not None or envelope.session_memory is not None
        ):
            desired = checkpoint_from_envelope(user_id, session_id, envelope)
            current = await anyio.to_thread.run_sync(
                self.journals.read_latest_compaction_checkpoint,
                user_id,
                session_id,
            )
            if current is None or current.checkpoint_id != desired.checkpoint_id:
                await anyio.to_thread.run_sync(
                    self.journals.append_compaction_checkpoint,
                    user_id,
                    session_id,
                    desired,
                )
            checkpoint_id = desired.checkpoint_id
        return SessionActivationResult(False, latest, checkpoint_id, False)

    async def _read_journal(
        self, user_id: str, session_id: str, history_turns: int
    ) -> tuple[CompactionCheckpoint | None, int, tuple[MemoryEvent, ...]]:
        checkpoint = await anyio.to_thread.run_sync(
            self.journals.read_latest_compaction_checkpoint,
            user_id,
            session_id,
        )
        latest = await anyio.to_thread.run_sync(
            self.journals.latest_original_sequence,
            user_id,
            session_id,
        )
        recent = await anyio.to_thread.run_sync(
            self.journals.read_recent_originals,
            user_id,
            session_id,
            history_turns,
        )
        return checkpoint, latest, recent

    def _restore_envelope(
        self,
        checkpoint: CompactionCheckpoint | None,
        recent: tuple[MemoryEvent, ...],
    ) -> MemorySummaryEnvelope | None:
        if checkpoint is None:
            return None
        envelope = checkpoint_to_envelope(checkpoint)
        if envelope.active_revision is None and envelope.session_memory is not None:
            envelope = envelope.model_copy(
                update={
                    "active_revision": materialize_session_memory_recovery_revision(
                        session_memory=envelope.session_memory,
                        recent_originals=recent,
                        now=self.clock(),
                    )
                }
            )
        return envelope

    async def _wait_for_projection(
        self, user_id: str, session_id: str
    ) -> SessionActivationResult:
        deadline = anyio.current_time() + self.activation_timeout_seconds
        while anyio.current_time() < deadline:
            if await self.store.read_latest_sequence(user_id, session_id) > 0:
                return await self._warm_result(user_id, session_id)
            await anyio.sleep(0.1)
        raise SessionActivationUnavailableError(
            "historical session activation timed out"
        )

    @staticmethod
    def _rebuild_job(
        user_id: str,
        session_id: str,
        latest_sequence: int,
        envelope: MemorySummaryEnvelope | None,
    ) -> CompressionJob:
        expected_version = envelope.version if envelope is not None else 0
        identity = (
            f"{user_id}\n{session_id}\n{expected_version}\n"
            f"{latest_sequence}\nTrue\nFalse"
        )
        return CompressionJob(
            job_id=f"memory-{uuid5(NAMESPACE_URL, identity).hex}",
            user_id=user_id,
            session_id=session_id,
            expected_version=expected_version,
            requested_through_sequence=latest_sequence,
            rebuild=True,
        )

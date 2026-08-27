"""Write-ahead memory use cases with non-blocking compression intent."""

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
import time
from typing import Any, Callable
from uuid import uuid5, NAMESPACE_URL

import anyio
from redis.exceptions import RedisError

from short_term_memory.compression.ccr_recall import (
    CcrRecallClient,
    CcrRecallError,
    extract_marker_hashes,
)
from short_term_memory.compression.generations import GenerationAssembler
from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.compression.session_memory_state import should_extract_memory
from short_term_memory.jobs.session_memory_queue import SessionMemoryJob
from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.jobs.redis_compression_queue import CompressionJob
from short_term_memory.models import MemoryEvent, MemorySummaryEnvelope
from short_term_memory.ports import RebuildCompletionWaiter
from short_term_memory.service.schemas import (
    EffectiveMemoryConfig,
    HeadroomProxyContext,
    MemoryReadRequest,
    MemoryReadResponse,
    MemoryReadState,
    MemoryRecallRequest,
    MemoryRecallResponse,
    MemoryRecallResult,
    MemoryTranscriptGrepRequest,
    MemoryTranscriptGrepResponse,
    MemoryTranscriptReadRequest,
    MemoryTranscriptReadResponse,
    MemoryWriteRequest,
    MemoryWriteResponse,
    ReadTiming,
    WriteTiming,
)
from short_term_memory.storage.async_redis_memory_store import EventConflictError
from short_term_memory.storage.journal_store import JournalConflictError, JournalStore
from short_term_memory.transcript.grep_tool import grep_transcript
from short_term_memory.transcript.journal_transcript import JournalTranscript
from short_term_memory.transcript.read_tool import read_transcript


TRANSCRIPT_MAX_RESPONSE_CHARS = 20_000


class RetryableWriteError(RuntimeError):
    """A journaled event could not yet be committed to Redis."""

    def __init__(self, event_id: str, committed_event_ids: tuple[str, ...]) -> None:
        super().__init__(f"Redis commit failed for event_id {event_id!r}; retry safely")
        self.event_id = event_id
        self.committed_event_ids = committed_event_ids


class MemoryReadUnavailableError(RuntimeError):
    """Neither Redis nor the durable journal can provide a safe read context."""


class MemoryTranscriptScopeError(PermissionError):
    """The supplied opaque scope does not belong to the requested session."""


class MemoryService:
    """Online use cases; it never invokes Headroom or a model provider."""

    def __init__(
        self,
        *,
        store: Any,
        journals: JournalStore,
        assembler: GenerationAssembler,
        compression_queue: Any,
        policy: HeadroomPolicy,
        scope_factory: OptimizationScopeFactory,
        settings: ShortTermMemorySettings,
        token_estimator: Any,
        headroom_proxy_url: str,
        clock: Callable[[], datetime] | None = None,
        policy_version: str = "v1",
        rebuild_waiter: RebuildCompletionWaiter | None = None,
        cold_rebuild_timeout_seconds: float | None = None,
        recall_client: CcrRecallClient | None = None,
        session_memory_queue: Any | None = None,
    ) -> None:
        if not headroom_proxy_url:
            raise ValueError("headroom_proxy_url must not be blank")
        if not policy_version:
            raise ValueError("policy_version must not be blank")
        if cold_rebuild_timeout_seconds is not None and cold_rebuild_timeout_seconds <= 0:
            raise ValueError("cold_rebuild_timeout_seconds must be positive")
        self.store = store
        self.journals = journals
        self.assembler = assembler
        self.compression_queue = compression_queue
        self.policy = policy
        self.scope_factory = scope_factory
        self.settings = settings
        self.token_estimator = token_estimator
        self.headroom_proxy_url = headroom_proxy_url.rstrip("/")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy_version = policy_version
        self.rebuild_waiter = rebuild_waiter
        self.cold_rebuild_timeout_seconds = (
            cold_rebuild_timeout_seconds or settings.api.request_timeout_seconds
        )
        self.recall_client = recall_client
        self.session_memory_queue = session_memory_queue

    async def write(
        self, request: MemoryWriteRequest, request_id: str
    ) -> MemoryWriteResponse:
        """Journal each original before its idempotent Redis commit."""

        started = time.perf_counter()
        redis_seconds = 0.0
        journal_seconds = 0.0
        queue_seconds = 0.0
        sequences: list[int] = []
        duplicate_event_ids: list[str] = []
        committed_event_ids: list[str] = []
        committed_new_event = False
        committed_events: list[MemoryEvent] = []

        for input_event in request.events:
            digest = sha256(input_event.content.encode("utf-8")).hexdigest()
            redis_started = time.perf_counter()
            try:
                reservation = await self.store.reserve_event(
                    request.user_id, request.session_id, input_event.event_id, digest
                )
            except self._retryable_redis_errors as error:
                raise RetryableWriteError(
                    input_event.event_id, tuple(committed_event_ids)
                ) from error
            redis_seconds += time.perf_counter() - redis_started
            sequences.append(reservation.sequence)

            if reservation.state == "committed":
                duplicate_event_ids.append(input_event.event_id)
                continue

            event = MemoryEvent(
                sequence=reservation.sequence,
                event_id=input_event.event_id,
                role=input_event.role,
                content_type=input_event.content_type,
                content=input_event.content,
                metadata=input_event.metadata,
                sha256=digest,
                created_at=self._now().isoformat(),
            )
            journal_started = time.perf_counter()
            try:
                append_result = await anyio.to_thread.run_sync(
                    self.journals.append_event,
                    request.user_id,
                    request.session_id,
                    event,
                )
            except JournalConflictError as error:
                raise EventConflictError(str(error)) from error
            journal_seconds += time.perf_counter() - journal_started
            if not append_result.appended:
                canonical = await anyio.to_thread.run_sync(
                    self.journals.find_event,
                    request.user_id,
                    request.session_id,
                    input_event.event_id,
                )
                if canonical is None:
                    raise ValueError("idempotent journal append has no canonical event")
                event = canonical

            redis_started = time.perf_counter()
            try:
                committed = await self.store.commit_event(
                    request.user_id, request.session_id, event
                )
            except self._retryable_redis_errors as error:
                raise RetryableWriteError(
                    input_event.event_id, tuple(committed_event_ids)
                ) from error
            redis_seconds += time.perf_counter() - redis_started
            if committed == "duplicate":
                duplicate_event_ids.append(input_event.event_id)
            else:
                committed_event_ids.append(input_event.event_id)
                committed_new_event = True
                committed_events.append(event)

        originals: tuple[MemoryEvent, ...] = ()
        compressible: tuple[MemoryEvent, ...] = ()
        should_compress = False
        should_evict_oldest_generation = False
        if committed_new_event:
            redis_started = time.perf_counter()
            originals, envelope = await asyncio.gather(
                self.store.read_originals_after(request.user_id, request.session_id, 0),
                self.store.read_envelope(request.user_id, request.session_id),
            )
            redis_seconds += time.perf_counter() - redis_started

            # Retain the most recent originals up to a token budget of
            # context_window * retain_ratio (default 25%).  Originals beyond that
            # budget are eligible for normal compression.
            retain_budget = int(
                self.settings.redis_session.context_window_tokens
                * self.settings.redis_session.retain_ratio
            )
            compressible = self._originals_beyond_budget(originals, retain_budget)

            # Normal compression: judge against the FULL context the model will
            # actually see — all originals (retained 25% + beyond-budget) plus the
            # compressed generations.  This makes the "context exceeds 60-70% of the
            # window" rule reflect reality.
            total_tokens = self._estimate_tokens(originals)
            if envelope is not None:
                total_tokens += self._estimate_generation_tokens(envelope)
            should_compress = self.policy.should_compress(
                estimated_tokens=total_tokens,
                message_count=len(compressible),
                session_seconds=request.session_seconds,
            )

            # Generation eviction is a storage-pressure fallback, not Claude compact.
            # It is independent of whether new originals are eligible for Headroom.
            if envelope is not None and envelope.compression_generations:
                segment_tokens = self._estimate_generation_tokens(envelope)
                if self.policy.should_compress(
                    estimated_tokens=segment_tokens,
                    message_count=len(envelope.compression_generations),
                    session_seconds=request.session_seconds,
                ):
                    should_evict_oldest_generation = True
        else:
            envelope = None
        # Original-only Headroom compression and generation eviction are independent.
        if should_compress and compressible:
            queue_started = time.perf_counter()
            await self.compression_queue.enqueue(
                self._compression_job(
                    request.user_id,
                    request.session_id,
                    envelope,
                    compressible[-1].sequence,
                    rebuild=False,
                )
            )
            queue_seconds += time.perf_counter() - queue_started
        if should_evict_oldest_generation:
            queue_started = time.perf_counter()
            await self.compression_queue.enqueue(
                self._compression_job(
                    request.user_id,
                    request.session_id,
                    envelope,
                    envelope.compressed_through_sequence,
                    rebuild=False,
                    evict_oldest_generation=True,
                )
            )
            queue_seconds += time.perf_counter() - queue_started

        last_committed = committed_events[-1] if committed_events else None
        if (
            self.session_memory_queue is not None
            and last_committed is not None
            and last_committed.role.value == "assistant"
        ):
            memory = envelope.session_memory if envelope is not None else None
            covered = memory.covered_through_sequence if memory is not None else 0
            tool_calls = sum(
                int(event.metadata.get("tool_call_count", "0") or 0)
                for event in originals
                if event.sequence > covered
            )
            last_has_tools = (
                last_committed.metadata.get("has_tool_calls", "").casefold()
                in {"1", "true", "yes"}
            )
            if should_extract_memory(
                current_token_count=total_tokens,
                tokens_at_last_extraction=memory.token_count if memory else 0,
                tool_calls_since_update=tool_calls,
                last_assistant_turn_has_tool_calls=last_has_tools,
                initialized=memory is not None,
            ):
                queue_started = time.perf_counter()
                await self.session_memory_queue.enqueue(
                    SessionMemoryJob(
                        user_id=request.user_id,
                        session_id=request.session_id,
                        expected_version=envelope.version if envelope else 0,
                        requested_through_sequence=last_committed.sequence,
                    )
                )
                queue_seconds += time.perf_counter() - queue_started

        return MemoryWriteResponse(
            request_id=request_id,
            accepted=True,
            sequence_from=min(sequences) if sequences else None,
            sequence_through=max(sequences) if sequences else None,
            duplicate_event_ids=duplicate_event_ids,
            compression_queued=should_compress and bool(originals),
            policy_version=self.policy_version,
            timing_ms=WriteTiming(
                total=self._milliseconds(started),
                redis=redis_seconds * 1_000,
                journal=journal_seconds * 1_000,
                queue=queue_seconds * 1_000,
            ),
        )

    async def read(
        self, request: MemoryReadRequest, request_id: str
    ) -> MemoryReadResponse:
        """Assemble Redis context, recovering originals from the journal if needed."""

        started = time.perf_counter()
        redis_started = time.perf_counter()
        history_turns = request.history_turns or self.settings.redis_session.history_turns
        envelope_result, originals_result = await asyncio.gather(
            self.store.read_envelope(request.user_id, request.session_id),
            self.store.read_recent_originals(
                request.user_id, request.session_id, history_turns
            ),
            return_exceptions=True,
        )
        redis_seconds = time.perf_counter() - redis_started
        self._raise_non_infrastructure(envelope_result)
        self._raise_non_infrastructure(originals_result)
        envelope = None if isinstance(envelope_result, Exception) else envelope_result
        originals = () if isinstance(originals_result, Exception) else originals_result
        redis_failed = isinstance(envelope_result, Exception) or isinstance(
            originals_result, Exception
        )
        recovery_seconds = 0.0
        source = "redis"

        # History view: when the caller opens a historical session (history=true),
        # return only the compressed summary (semantic summary + marker segments),
        # never restore the full original journal, so history cannot fill the context.
        if request.history:
            originals = ()
        elif not originals and envelope is None:
            recovery_started = time.perf_counter()
            originals = await anyio.to_thread.run_sync(
                self.journals.read_recent_originals,
                request.user_id,
                request.session_id,
                history_turns,
            )
            if originals:
                try:
                    restored = await self.store.restore_originals(
                        request.user_id, request.session_id, originals
                    )
                except self._retryable_redis_errors:
                    restored = True
                    envelope = None
                if not restored:
                    refreshed_envelope, refreshed_originals = await asyncio.gather(
                        self.store.read_envelope(request.user_id, request.session_id),
                        self.store.read_recent_originals(
                            request.user_id, request.session_id, history_turns
                        ),
                        return_exceptions=True,
                    )
                    self._raise_non_infrastructure(refreshed_envelope)
                    self._raise_non_infrastructure(refreshed_originals)
                    if not isinstance(refreshed_envelope, Exception):
                        envelope = refreshed_envelope
                    if not isinstance(refreshed_originals, Exception) and refreshed_originals:
                        originals = refreshed_originals
            recovery_seconds = time.perf_counter() - recovery_started
            if originals:
                source = "journal_rebuild"
            elif redis_failed:
                raise MemoryReadUnavailableError(
                    "Redis read failed and journal has no recoverable originals"
                )

        now = self._now()
        latest_sequence = max(
            max((event.sequence for event in originals), default=0),
            envelope.compressed_through_sequence if envelope is not None else 0,
        )
        expired = self._has_expired_generation(envelope, now)
        if latest_sequence and (envelope is None or self._requires_rebuild(envelope, now)):
            through_sequence = max(
                latest_sequence,
                envelope.compressed_through_sequence if envelope is not None else 0,
            )
            if through_sequence:
                job = self._compression_job(
                    request.user_id,
                    request.session_id,
                    envelope,
                    through_sequence,
                    rebuild=True,
                )
                try:
                    await self.compression_queue.enqueue(
                        job
                    )
                except self._retryable_redis_errors as error:
                    if expired:
                        raise MemoryReadUnavailableError(
                            "cold rebuild enqueue is unavailable"
                        ) from error
                    if source != "journal_rebuild":
                        raise
                if expired:
                    cold_started = time.perf_counter()
                    envelope = await self._wait_for_cold_rebuild(job)
                    recovery_seconds += time.perf_counter() - cold_started
                    source = "journal_rebuild"
                    latest_sequence = max(
                        latest_sequence, envelope.compressed_through_sequence
                    )

        assembly_started = time.perf_counter()
        messages = self.assembler.build_read_messages(envelope, originals, now)
        assembly_seconds = time.perf_counter() - assembly_started
        scope = self.scope_factory.for_session(request.user_id, request.session_id)
        config = self._effective_config() if request.include_effective_config else None
        ccr_markers = list(extract_marker_hashes(messages))

        return MemoryReadResponse(
            request_id=request_id,
            messages=list(messages),
            memory=MemoryReadState(
                compressed_through_sequence=(
                    envelope.compressed_through_sequence if envelope is not None else 0
                ),
                latest_sequence=latest_sequence,
                source=source,
                compression_segments=(
                    len(self.assembler._fresh_generations(envelope, now))
                    if envelope is not None
                    else 0
                ),
            ),
            headroom=HeadroomProxyContext(
                proxy_url=self.headroom_proxy_url,
                scope_headers=scope.as_headroom_headers(),
            ),
            ccr_markers=ccr_markers,
            effective_config=config,
            timing_ms=ReadTiming(
                total=self._milliseconds(started),
                redis=redis_seconds * 1_000,
                recovery=recovery_seconds * 1_000,
                assembly=assembly_seconds * 1_000,
            ),
        )

    async def grep_transcript(
        self,
        request: MemoryTranscriptGrepRequest,
        request_id: str,
        *,
        session_scope: str,
    ) -> MemoryTranscriptGrepResponse:
        """Run Claude-style Grep against the authenticated session Journal."""

        self._validate_transcript_scope(
            request.user_id, request.session_id, session_scope
        )
        transcript_lines = await anyio.to_thread.run_sync(
            JournalTranscript(self.journals).lines,
            request.user_id,
            request.session_id,
        )
        result = grep_transcript(
            transcript_lines,
            request,
            max_response_chars=TRANSCRIPT_MAX_RESPONSE_CHARS,
        )
        return MemoryTranscriptGrepResponse(
            request_id=request_id, **result.model_dump()
        )

    async def read_transcript(
        self,
        request: MemoryTranscriptReadRequest,
        request_id: str,
        *,
        session_scope: str,
    ) -> MemoryTranscriptReadResponse:
        """Run Claude-style Read against the authenticated session Journal."""

        self._validate_transcript_scope(
            request.user_id, request.session_id, session_scope
        )
        transcript_lines = await anyio.to_thread.run_sync(
            JournalTranscript(self.journals).lines,
            request.user_id,
            request.session_id,
        )
        result = read_transcript(
            transcript_lines,
            request,
            max_response_chars=TRANSCRIPT_MAX_RESPONSE_CHARS,
        )
        return MemoryTranscriptReadResponse(
            request_id=request_id, **result.model_dump()
        )

    def _validate_transcript_scope(
        self, user_id: str, session_id: str, session_scope: str
    ) -> None:
        expected = self.scope_factory.for_session(user_id, session_id).session_scope
        if not secrets.compare_digest(session_scope, expected):
            raise MemoryTranscriptScopeError("session scope does not match")

    async def recall(
        self, request: MemoryRecallRequest, request_id: str
    ) -> MemoryRecallResponse:
        """Pull originals back from the CCR store by marker hash (application-driven).

        Each hash is resolved recursively, following re-compression chains down
        to the true original text. Tool choice and ordering belong to the model.
        """
        if self.recall_client is None:
            raise MemoryReadUnavailableError("recall is not configured")
        scope = self.scope_factory.for_session(request.user_id, request.session_id)
        scope_headers = scope.as_headroom_headers()

        hashes = list(request.hashes)

        results: list[MemoryRecallResult] = []
        for hash_value in hashes:
            try:
                content = await self.recall_client.recall_recursive(
                    hash_value, scope_headers=scope_headers
                )
                results.append(
                    MemoryRecallResult(hash=hash_value, content=content, recovered=True)
                )
            except CcrRecallError:
                results.append(
                    MemoryRecallResult(hash=hash_value, content="", recovered=False)
                )
        return MemoryRecallResponse(request_id=request_id, results=results)

    def _compression_job(
        self,
        user_id: str,
        session_id: str,
        envelope: MemorySummaryEnvelope | None,
        through_sequence: int,
        *,
        rebuild: bool,
        evict_oldest_generation: bool = False,
    ) -> CompressionJob:
        expected_version = envelope.version if envelope is not None else 0
        job_identity = (
            f"{user_id}\n{session_id}\n{expected_version}\n"
            f"{through_sequence}\n{rebuild}\n{evict_oldest_generation}"
        )
        return CompressionJob(
            job_id=f"memory-{uuid5(NAMESPACE_URL, job_identity).hex}",
            user_id=user_id,
            session_id=session_id,
            expected_version=expected_version,
            requested_through_sequence=through_sequence,
            rebuild=rebuild,
            evict_oldest_generation=evict_oldest_generation,
        )

    def _effective_config(self) -> EffectiveMemoryConfig:
        return EffectiveMemoryConfig(
            history_turns=self.settings.redis_session.history_turns,
            redis_ttl_seconds=self.settings.redis_session.ttl_seconds,
            ccr_ttl_seconds=self.settings.headroom_service.ccr_ttl_seconds,
            journal_retention_days=self.settings.journal.retention_days,
            trigger_ratio=self.settings.redis_session.trigger_ratio,
            policy_version=self.policy_version,
        )

    def _estimate_tokens(self, originals: tuple[MemoryEvent, ...]) -> int:
        messages = tuple(
            {"role": event.role.value, "content": event.content} for event in originals
        )
        estimator = self.token_estimator
        if hasattr(estimator, "estimate"):
            return int(estimator.estimate(messages))
        return int(estimator(messages))

    @staticmethod
    def _originals_beyond_budget(
        originals: tuple[MemoryEvent, ...], retain_budget: int
    ) -> tuple[MemoryEvent, ...]:
        """Return originals beyond the retained token budget.

        Keeps the most recent originals up to ``retain_budget`` tokens; the older
        remainder (in the front) is returned as compressible.
        """
        if retain_budget <= 0 or not originals:
            return originals
        # originals are in ascending sequence order; the newest are at the end.
        # Walk from the newest backwards, accumulating tokens until the budget fills.
        accumulate = 0
        cut_index = 0
        for i in range(len(originals) - 1, -1, -1):
            accumulate += len(originals[i].content)
            if accumulate > retain_budget:
                cut_index = i
                break
        return originals[:cut_index]

    def _estimate_generation_tokens(
        self, envelope: MemorySummaryEnvelope
    ) -> int:
        """Estimate tokens of the compressed generations that stay in context."""
        messages: list[dict[str, Any]] = []
        for gen in envelope.compression_generations:
            for m in gen.messages:
                content = m.content
                if isinstance(content, str):
                    messages.append({"role": m.role, "content": content})
        if not messages:
            return 0
        estimator = self.token_estimator
        if hasattr(estimator, "estimate"):
            return int(estimator.estimate(tuple(messages)))
        return int(estimator(tuple(messages)))

    def _requires_rebuild(
        self, envelope: MemorySummaryEnvelope | None, now: datetime
    ) -> bool:
        if envelope is None:
            return False
        refresh_at = now + timedelta(
            seconds=self.settings.headroom_service.ccr_refresh_seconds
        )
        return any(
            self._aware_datetime(generation.ccr_expires_at) <= refresh_at
            for generation in envelope.compression_generations
        )

    def _has_expired_generation(
        self, envelope: MemorySummaryEnvelope | None, now: datetime
    ) -> bool:
        return bool(
            envelope is not None
            and any(
                self._aware_datetime(generation.ccr_expires_at) <= now
                for generation in envelope.compression_generations
            )
        )

    async def _wait_for_cold_rebuild(
        self, job: CompressionJob
    ) -> MemorySummaryEnvelope:
        if self.rebuild_waiter is None:
            raise MemoryReadUnavailableError("cold rebuild worker is unavailable")
        try:
            async with asyncio.timeout(self.cold_rebuild_timeout_seconds):
                rebuilt = await self.rebuild_waiter.wait_for(
                    job, self.cold_rebuild_timeout_seconds
                )
        except (TimeoutError, *self._retryable_redis_errors) as error:
            raise MemoryReadUnavailableError("cold rebuild is unavailable") from error
        now = self._now()
        if (
            rebuilt is None
            or rebuilt.version <= job.expected_version
            or rebuilt.compressed_through_sequence < job.requested_through_sequence
            or self._has_expired_generation(rebuilt, now)
            or not any(
                generation.from_sequence <= 1
                and generation.through_sequence >= job.requested_through_sequence
                and self._aware_datetime(generation.ccr_expires_at) > now
                for generation in rebuilt.compression_generations
            )
        ):
            raise MemoryReadUnavailableError("cold rebuild did not produce fresh context")
        return rebuilt

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _aware_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("CCR timestamps must be timezone-aware")
        return parsed

    @staticmethod
    def _milliseconds(started: float) -> float:
        return (time.perf_counter() - started) * 1_000

    _retryable_redis_errors = (RedisError, OSError, TimeoutError, ConnectionError)

    def _raise_non_infrastructure(self, result: object) -> None:
        if isinstance(result, Exception) and not isinstance(
            result, self._retryable_redis_errors
        ):
            raise result

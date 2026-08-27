"""Request-time Claude context preparation with a bounded Redis CAS."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid
from typing import Any

import anyio

from short_term_memory.compression.auto_compact import (
    AutoCompactContext,
    ModelProfile,
    auto_compact_if_needed,
    effective_context_window,
)
from short_term_memory.compression.context_messages import to_provider_messages
from short_term_memory.compression.context_query import (
    apply_compaction_result,
    load_active_messages,
)
from short_term_memory.compression.micro_compact import (
    TimeBasedMicroCompactConfig,
    microcompact_messages,
)
from short_term_memory.models import (
    AutoCompactTrackingState,
    CompactBoundary,
    ContextRevision,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
)
from short_term_memory.service.schemas import HeadroomProxyContext
from short_term_memory.storage.compaction_checkpoint import checkpoint_from_envelope
from short_term_memory.transcript.tool_definitions import TRANSCRIPT_TOOL_DEFINITIONS


class ContextCompactionUnavailableError(RuntimeError):
    """The prompt still exceeds the effective model window after compaction."""


@dataclass(frozen=True)
class PreparedContext:
    messages: tuple[SessionCompressionMessage, ...]
    tools: tuple[dict[str, Any], ...]
    headroom: HeadroomProxyContext
    was_compacted: bool
    boundary: CompactBoundary | None


class ContextCoordinator:
    def __init__(
        self,
        *,
        store: Any,
        checkpoint_journal: Any,
        token_estimator: Any,
        auto_context_factory: Callable[
            [ModelProfile, str, object | None], AutoCompactContext
        ],
        history_turns: int,
        headroom_proxy_url: str,
        scope_headers_factory: Callable[[str, str], dict[str, str]],
        microcompact_config: TimeBasedMicroCompactConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if history_turns < 1:
            raise ValueError("history_turns must be positive")
        if not headroom_proxy_url:
            raise ValueError("headroom_proxy_url must not be blank")
        self.store = store
        self.checkpoint_journal = checkpoint_journal
        self.token_estimator = token_estimator
        self.auto_context_factory = auto_context_factory
        self.history_turns = history_turns
        self.headroom_proxy_url = headroom_proxy_url.rstrip("/")
        self.scope_headers_factory = scope_headers_factory
        self.microcompact_config = (
            microcompact_config or TimeBasedMicroCompactConfig()
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def prepare(
        self,
        *,
        user_id: str,
        session_id: str,
        model_profile: ModelProfile,
        query_source: str = "main",
        history_turns: int | None = None,
    ) -> PreparedContext:
        turns = history_turns or self.history_turns
        envelope, originals = await self._read(user_id, session_id, turns)
        now = self._now()
        current = load_active_messages(envelope, originals, now)
        current = microcompact_messages(
            current,
            query_source,
            now=now,
            config=self.microcompact_config,
        ).messages
        token = uuid.uuid4().hex
        acquired = await self.store.acquire_context_compaction_lease(
            user_id, session_id, token
        )
        if not acquired:
            return self._prepared(
                current, model_profile, user_id, session_id, False
            )
        try:
            tracking = (
                envelope.auto_compact_tracking
                if envelope is not None
                else AutoCompactTrackingState()
            )
            auto_context = self.auto_context_factory(
                model_profile,
                query_source,
                envelope.session_memory if envelope is not None else None,
            )
            compacted = await auto_compact_if_needed(
                messages=current,
                context=auto_context,
                tracking=tracking,
            )
            if not compacted.was_compacted and compacted.tracking == tracking:
                return self._prepared(
                    current, model_profile, user_id, session_id, False
                )
            next_envelope = self._next_envelope(
                envelope, compacted.tracking, compacted.compaction_result
            )
            expected = envelope.version if envelope is not None else 0
            written = await self.store.compare_and_set_envelope(
                user_id, session_id, expected, next_envelope
            )
            if not written:
                reloaded, recent = await self._read(user_id, session_id, turns)
                reloaded_at = self._now()
                safe = load_active_messages(reloaded, recent, reloaded_at)
                safe = microcompact_messages(
                    safe,
                    query_source,
                    now=reloaded_at,
                    config=self.microcompact_config,
                ).messages
                return self._prepared(
                    safe, model_profile, user_id, session_id, False
                )
            if compacted.compaction_result is not None:
                checkpoint = checkpoint_from_envelope(
                    user_id, session_id, next_envelope
                )
                await anyio.to_thread.run_sync(
                    self.checkpoint_journal.append_compaction_checkpoint,
                    user_id,
                    session_id,
                    checkpoint,
                )
            messages = (
                apply_compaction_result(current, compacted.compaction_result)
                if compacted.compaction_result is not None
                else current
            )
            return self._prepared(
                messages,
                model_profile,
                user_id,
                session_id,
                compacted.was_compacted,
            )
        finally:
            await self.store.release_context_compaction_lease(
                user_id, session_id, token
            )

    async def _read(self, user: str, session: str, turns: int):
        envelope = await self.store.read_envelope(user, session)
        originals = await self.store.read_recent_originals(user, session, turns)
        return envelope, originals

    def _next_envelope(self, current, tracking, compaction_result):
        expected = current.version if current is not None else 0
        revision = current.active_revision if current is not None else None
        if compaction_result is not None:
            raw = (compaction_result.boundary_marker.model_extra or {}).get(
                "compact_boundary"
            )
            boundary = CompactBoundary.model_validate(raw)
            previous_revision = revision.version if revision is not None else 0
            covered_ids = tuple(
                generation.generation
                for generation in (current.compression_generations if current else ())
                if generation.through_sequence <= boundary.covered_through_sequence
            )
            revision = ContextRevision(
                version=previous_revision + 1,
                boundary=boundary,
                summary_message=compaction_result.summary_messages[-1],
                messages_to_keep=compaction_result.messages_to_keep,
                covered_generation_ids=covered_ids,
                updated_at=self._now().isoformat(),
            )
        if current is None:
            return MemorySummaryEnvelope(
                version=1,
                compressed_through_sequence=0,
                active_revision=revision,
                auto_compact_tracking=tracking,
                updated_at=self._now().isoformat(),
            )
        return current.model_copy(
            update={
                "version": expected + 1,
                "active_revision": revision,
                "auto_compact_tracking": tracking,
                "updated_at": self._now().isoformat(),
            }
        )

    def _prepared(
        self, messages, profile, user, session, compacted
    ) -> PreparedContext:
        tokens = self.token_estimator.estimate(to_provider_messages(messages))
        if tokens > effective_context_window(profile) and not compacted:
            raise ContextCompactionUnavailableError(
                "context compaction could not satisfy the effective model window"
            )
        boundary = None
        if messages:
            raw = (messages[0].model_extra or {}).get("compact_boundary")
            if raw:
                boundary = CompactBoundary.model_validate(raw)
        return PreparedContext(
            messages=messages,
            tools=TRANSCRIPT_TOOL_DEFINITIONS,
            headroom=HeadroomProxyContext(
                proxy_url=self.headroom_proxy_url,
                scope_headers=self.scope_headers_factory(user, session),
            ),
            was_compacted=compacted,
            boundary=boundary,
        )

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from short_term_memory.compression.auto_compact import (
    AutoCompactContext,
    ModelProfile,
)
from short_term_memory.compression.micro_compact import (
    TIME_BASED_MC_CLEARED_MESSAGE,
    TimeBasedMicroCompactConfig,
)
from short_term_memory.compression.session_memory_compact import CompactionResult
from short_term_memory.models import (
    AutoCompactTrackingState,
    CompressionGeneration,
    MemoryEvent,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
)
from short_term_memory.service.context_coordinator import (
    ContextCompactionUnavailableError,
    ContextCoordinator,
)


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


class Estimator:
    def __init__(self, tokens):
        self.tokens = tokens

    def estimate(self, messages):
        del messages
        return self.tokens


class Store:
    def __init__(self, envelope=None, events=()):
        self.envelope = envelope
        self.events = events
        self.locked = False
        self.cas_conflict = False

    async def read_envelope(self, user_id, session_id):
        del user_id, session_id
        return self.envelope

    async def read_recent_originals(self, user_id, session_id, history_turns):
        del user_id, session_id, history_turns
        return self.events

    async def acquire_context_compaction_lease(self, user_id, session_id, token):
        del user_id, session_id, token
        if self.locked:
            return False
        self.locked = True
        return True

    async def release_context_compaction_lease(self, user_id, session_id, token):
        del user_id, session_id, token
        self.locked = False
        return True

    async def compare_and_set_envelope(
        self, user_id, session_id, expected_version, envelope
    ):
        del user_id, session_id
        if self.cas_conflict:
            self.cas_conflict = False
            self.envelope = self.envelope.model_copy(
                update={"version": self.envelope.version + 1}
            )
            return False
        current = self.envelope.version if self.envelope else 0
        if current != expected_version:
            return False
        self.envelope = envelope
        return True


class RecordingCheckpointJournal:
    def __init__(self) -> None:
        self.checkpoints = []

    def append_compaction_checkpoint(self, user_id, session_id, checkpoint):
        assert (user_id, session_id) == ("u", "s")
        self.checkpoints.append(checkpoint)


def event(sequence, content):
    return MemoryEvent(
        sequence=sequence,
        event_id=f"e{sequence}",
        role="user",
        content_type="conversation",
        content=content,
        metadata={},
        sha256=sha256(content.encode()).hexdigest(),
        created_at=NOW.isoformat(),
    )


def envelope(version=1):
    return MemorySummaryEnvelope(
        version=version,
        compressed_through_sequence=0,
        auto_compact_tracking=AutoCompactTrackingState(),
        updated_at=NOW.isoformat(),
    )


def result(summary="new summary"):
    boundary = SessionCompressionMessage(
        role="system",
        content="boundary",
        compact_boundary={
            "boundary_id": "b1",
            "trigger": "auto",
            "strategy": "traditional",
            "covered_through_sequence": 1,
            "pre_compact_tokens": 180_000,
            "true_post_compact_tokens": 1_000,
            "created_at": NOW.isoformat(),
        },
    )
    return CompactionResult(
        boundary_marker=boundary,
        summary_messages=(SessionCompressionMessage(role="user", content=summary),),
        messages_to_keep=(),
        true_post_compact_token_count=1_000,
    )


def coordinator(
    store,
    *,
    tokens,
    compact=None,
    calls=None,
    microcompact_config=None,
    checkpoint_journal=None,
):
    calls = calls if calls is not None else []
    estimator = Estimator(tokens)

    def factory(profile, query_source, session_memory):
        del session_memory

        async def l4(messages, threshold):
            del messages, threshold
            calls.append("l4")
            return compact

        async def l3(messages, tracking):
            del messages, tracking
            calls.append("l3")
            return compact or result()

        return AutoCompactContext(
            model_profile=profile,
            token_estimator=estimator,
            query_source=query_source,
            try_session_memory=l4,
            compact_conversation=l3,
        )

    return ContextCoordinator(
        store=store,
        checkpoint_journal=checkpoint_journal or RecordingCheckpointJournal(),
        token_estimator=estimator,
        auto_context_factory=factory,
        history_turns=10,
        headroom_proxy_url="http://headroom/v1",
        scope_headers_factory=lambda user, session: {"session": f"{user}:{session}"},
        microcompact_config=microcompact_config,
        clock=lambda: NOW,
    )


PROFILE = ModelProfile(context_window_tokens=200_000, max_output_tokens=32_000)


@pytest.mark.asyncio
async def test_prepare_persists_replacement_and_returns_only_post_compact_messages() -> None:
    store = Store(envelope(), (event(1, "old generation"),))
    checkpoint_journal = RecordingCheckpointJournal()
    prepared = await coordinator(
        store,
        tokens=180_000,
        compact=result(),
        checkpoint_journal=checkpoint_journal,
    ).prepare(
        user_id="u", session_id="s", model_profile=PROFILE
    )
    assert prepared.was_compacted
    assert prepared.messages[0].model_extra["compact_boundary"] is not None
    assert [item.content for item in prepared.messages].count("new summary") == 1
    assert "old generation" not in [item.content for item in prepared.messages]
    assert store.envelope.active_revision.summary_message.content == "new summary"
    assert len(checkpoint_journal.checkpoints) == 1
    assert checkpoint_journal.checkpoints[0].active_revision == store.envelope.active_revision


@pytest.mark.asyncio
async def test_below_threshold_returns_existing_context_without_model_calls() -> None:
    calls = []
    store = Store(envelope(), (event(1, "recent"),))
    prepared = await coordinator(store, tokens=1_000, calls=calls).prepare(
        user_id="u", session_id="s", model_profile=PROFILE
    )
    assert not prepared.was_compacted
    assert [item.content for item in prepared.messages] == ["recent"]
    assert calls == []
    assert len(prepared.tools) == 2
    assert prepared.headroom.proxy_url == "http://headroom/v1"


@pytest.mark.asyncio
async def test_l1_clears_only_request_projection_before_l2_without_mutating_storage() -> None:
    old = NOW - timedelta(hours=2)
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=4,
        messages=(
            SessionCompressionMessage(
                role="assistant",
                content=({"type": "tool_use", "id": "old", "name": "Read", "input": {}},),
                stm_timestamp=old.isoformat(),
            ),
            SessionCompressionMessage(
                role="user",
                content=({"type": "tool_result", "tool_use_id": "old", "content": "OLD FULL"},),
            ),
            SessionCompressionMessage(
                role="assistant",
                content=({"type": "tool_use", "id": "new", "name": "Grep", "input": {}},),
                stm_timestamp=old.isoformat(),
            ),
            SessionCompressionMessage(
                role="user",
                content=({"type": "tool_result", "tool_use_id": "new", "content": "NEW FULL"},),
            ),
        ),
        tokens_before=100,
        tokens_after=50,
        created_at=old.isoformat(),
        ccr_expires_at=(NOW + timedelta(hours=1)).isoformat(),
    )
    original_envelope = envelope().model_copy(
        update={"compressed_through_sequence": 4, "compression_generations": (generation,)}
    )
    journal_event = event(5, "JOURNAL EXACT")
    store = Store(original_envelope, (journal_event,))
    prepared = await coordinator(
        store,
        tokens=1_000,
        microcompact_config=TimeBasedMicroCompactConfig(
            enabled=True, gap_threshold_minutes=30, keep_recent=1
        ),
    ).prepare(user_id="u", session_id="s", model_profile=PROFILE, query_source="main")

    old_result = prepared.messages[1].content[0]["content"]
    new_result = prepared.messages[3].content[0]["content"]
    assert old_result == TIME_BASED_MC_CLEARED_MESSAGE
    assert new_result == "NEW FULL"
    assert store.envelope is original_envelope
    assert store.envelope.compression_generations[0].messages[1].content[0]["content"] == "OLD FULL"
    assert store.events[0].content == "JOURNAL EXACT"


@pytest.mark.asyncio
async def test_cas_conflict_reloads_once_without_second_model_call() -> None:
    calls = []
    checkpoint_journal = RecordingCheckpointJournal()
    store = Store(envelope(), (event(1, "recent"),))
    store.cas_conflict = True
    prepared = await coordinator(
        store,
        tokens=180_000,
        compact=result(),
        calls=calls,
        checkpoint_journal=checkpoint_journal,
    ).prepare(user_id="u", session_id="s", model_profile=PROFILE)
    assert calls == ["l4"]
    assert not prepared.was_compacted
    assert store.envelope.active_revision is None
    assert checkpoint_journal.checkpoints == []


@pytest.mark.asyncio
async def test_failure_above_effective_window_is_unavailable() -> None:
    calls = []
    store = Store(envelope(), (event(1, "huge"),))
    coord = coordinator(store, tokens=181_000, compact=None, calls=calls)

    async def broken_factory(profile, query_source, session_memory):
        del profile, query_source, session_memory

    del broken_factory
    original = coord.auto_context_factory

    def factory(profile, query_source, session_memory):
        ctx = original(profile, query_source, session_memory)

        async def fail(messages, tracking):
            del messages, tracking
            raise RuntimeError("failed")

        return AutoCompactContext(
            model_profile=ctx.model_profile,
            token_estimator=ctx.token_estimator,
            query_source=ctx.query_source,
            try_session_memory=ctx.try_session_memory,
            compact_conversation=fail,
        )

    coord.auto_context_factory = factory
    with pytest.raises(ContextCompactionUnavailableError):
        await coord.prepare(user_id="u", session_id="s", model_profile=PROFILE)

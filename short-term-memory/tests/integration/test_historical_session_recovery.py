from dataclasses import replace
from datetime import datetime, timezone
import json

import httpx
import pytest

from short_term_memory.agent.agent_chat import AgentChatClient
from short_term_memory.compression.auto_compact import AutoCompactContext
from short_term_memory.compression.ccr_recall import (
    CcrRecallError,
    extract_marker_hashes,
)
from short_term_memory.compression.generations import (
    GenerationAssembler,
    GenerationPlanner,
)
from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.jobs.compression_worker import CompressionWorker
from short_term_memory.jobs.redis_compression_queue import CompressionJobLease
from short_term_memory.models import (
    CompactBoundary,
    ContextRevision,
    HeadroomCompressionResult,
    HeadroomCompressionStatus,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
    SessionMemoryRevision,
)
from short_term_memory.service.app import create_app
from short_term_memory.service.context_coordinator import ContextCoordinator
from short_term_memory.service.memory_service import MemoryService
from short_term_memory.service.session_activation import SessionActivator
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.compaction_checkpoint import checkpoint_from_envelope
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.factories import memory_event
from tests.storage.fake_redis import AsyncFakeRedis


NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
MARKER_HASH = "abc123def456"


class Estimator:
    def estimate(self, messages) -> int:
        return sum(max(1, len(str(message.get("content", ""))) // 4) for message in messages)


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs = []
        self.acked = []

    async def enqueue(self, job):
        self.jobs.append(job)
        return "ready"

    async def ack(self, lease):
        self.acked.append(lease.job.job_id)
        return True


class CapturingModel:
    def __init__(self, outputs=None) -> None:
        self.calls = []
        self.outputs = iter(outputs or ({"content": "continued", "tool_calls": []},))

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.outputs)


class SuccessfulHeadroom:
    async def compress(self, messages, **kwargs):
        assert messages[0]["content"] == "message-1"
        assert messages[-1]["content"] == "TTL is 43200"
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.SUCCESS,
            messages=(
                {
                    "role": "assistant",
                    "content": (
                        "[180 items compressed. Retrieve more: "
                        f"hash={MARKER_HASH}]"
                    ),
                },
            ),
            fallback_used=False,
            tokens_before=10_000,
            tokens_after=500,
        )


class Recall:
    def __init__(self) -> None:
        self.fail = False

    async def recall_recursive(self, hash_value, **kwargs):
        assert hash_value == MARKER_HASH
        if self.fail:
            raise CcrRecallError("expired")
        return "EXACT CCR ORIGINAL"


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def continuity_envelope() -> MemorySummaryEnvelope:
    boundary = CompactBoundary(
        boundary_id="historical-boundary",
        trigger="auto",
        strategy="session_memory",
        covered_through_sequence=170,
        pre_compact_tokens=20_000,
        true_post_compact_tokens=1_000,
        created_at=NOW.isoformat(),
    )
    return MemorySummaryEnvelope(
        version=7,
        compressed_through_sequence=170,
        session_memory=SessionMemoryRevision(
            version=2,
            content="RECOVERED SESSION MEMORY",
            covered_through_sequence=170,
            token_count=500,
            updated_at=NOW.isoformat(),
        ),
        active_revision=ContextRevision(
            version=3,
            boundary=boundary,
            summary_message=SessionCompressionMessage(
                role="user", content="RECOVERED CONTINUITY"
            ),
            updated_at=NOW.isoformat(),
        ),
        updated_at=NOW.isoformat(),
    )


async def no_l4(messages, threshold):
    return None


async def no_l3(messages, tracking):
    raise AssertionError("small recovered context must not compact again")


@pytest.fixture
def stack(tmp_path):
    settings = ShortTermMemorySettings(home=str(tmp_path), environment="development")
    settings = replace(
        settings,
        continuity_compaction=replace(settings.continuity_compaction, enabled=False),
    )
    redis = AsyncFakeRedis()
    store = AsyncRedisMemoryStore(redis)
    journals = JournalStore(VFSAdapter(tmp_path))
    queue = RecordingQueue()
    scope_factory = OptimizationScopeFactory("test-secret")
    estimator = Estimator()
    recall = Recall()
    memory_service = MemoryService(
        store=store,
        journals=journals,
        assembler=GenerationAssembler(max_segments=8),
        compression_queue=queue,
        policy=HeadroomPolicy(
            context_window_tokens=settings.redis_session.context_window_tokens,
            trigger_ratio=settings.redis_session.trigger_ratio,
            max_messages=settings.redis_session.max_messages,
            max_session_seconds=settings.redis_session.max_session_seconds,
        ),
        scope_factory=scope_factory,
        settings=settings,
        token_estimator=estimator,
        headroom_proxy_url="http://headroom/v1",
        clock=lambda: NOW,
        recall_client=recall,
    )

    def auto_context_factory(profile, query_source, session_memory):
        return AutoCompactContext(
            model_profile=profile,
            token_estimator=estimator,
            query_source=query_source,
            try_session_memory=no_l4,
            compact_conversation=no_l3,
        )

    coordinator = ContextCoordinator(
        store=store,
        checkpoint_journal=journals,
        token_estimator=estimator,
        auto_context_factory=auto_context_factory,
        history_turns=5,
        headroom_proxy_url="http://headroom/v1",
        scope_headers_factory=lambda user, session: scope_factory.for_session(
            user, session
        ).as_headroom_headers(),
        clock=lambda: NOW,
    )
    activator = SessionActivator(
        store=store,
        journals=journals,
        compression_queue=queue,
        history_turns=5,
        activation_timeout_seconds=1,
        clock=lambda: NOW,
    )
    app = create_app(lambda: memory_service, settings=settings)
    app.state.context_coordinator = coordinator
    app.state.session_activator = activator
    return {
        "app": app,
        "store": store,
        "journals": journals,
        "queue": queue,
        "scope_factory": scope_factory,
        "memory_service": memory_service,
        "activator": activator,
        "recall": recall,
    }


def seed_expired_history(stack) -> None:
    for sequence in range(1, 181):
        content = "TTL is 43200" if sequence == 180 else f"message-{sequence}"
        event = memory_event(
            sequence=sequence,
            event_id=f"event-{sequence}",
            content=content,
        )
        if sequence % 2 == 0:
            event = event.model_copy(update={"role": "assistant"})
        stack["journals"].append_event("u", "old", event)
    stack["journals"].append_compaction_checkpoint(
        "u",
        "old",
        checkpoint_from_envelope("u", "old", continuity_envelope()),
    )


@pytest.mark.asyncio
async def test_expired_redis_history_recovers_context_before_first_new_question(stack):
    seed_expired_history(stack)
    model = CapturingModel()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack["app"]), base_url="http://memory"
    ) as http:
        agent = AgentChatClient(
            memory_api_url="http://memory", model_call=model, http_client=http
        )
        answer = await agent.turn("u", "old", "what did we decide?", history_turns=5)

    assert answer == "continued"
    first_model_messages = model.calls[0]["messages"]
    assert any(
        "RECOVERED CONTINUITY" in str(message["content"])
        for message in first_model_messages
    )
    assert any(
        "what did we decide?" in str(message["content"])
        for message in first_model_messages
    )
    assert await stack["store"].read_latest_sequence("u", "old") == 182
    originals = stack["journals"].read_original_range("u", "old", 1, 2**63 - 1)
    assert len(originals) == len({event.sequence for event in originals}) == 182
    assert stack["queue"].jobs[0].rebuild is True
    assert stack["queue"].jobs[0].requested_through_sequence == 180


@pytest.mark.asyncio
async def test_activation_rebuilds_headroom_and_ccr_failure_falls_back_to_grep_read(stack):
    seed_expired_history(stack)
    result = await stack["activator"].activate("u", "old", history_turns=5)
    assert result.rebuild_queued
    job = stack["queue"].jobs[-1]
    worker = CompressionWorker(
        queue=stack["queue"],
        store=stack["store"],
        planner=GenerationPlanner(stack["store"], stack["journals"], max_segments=8),
        headroom=SuccessfulHeadroom(),
        compression_model="test-model",
        scope_factory=stack["scope_factory"],
        ccr_ttl_seconds=43_200,
        ccr_refresh_seconds=3_600,
        max_segments=8,
        clock=lambda: NOW,
    )

    completed = await worker._execute(CompressionJobLease(job, "lease"), NOW)

    assert completed.state == "acked"
    envelope = await stack["store"].read_envelope("u", "old")
    assert envelope is not None and envelope.compression_generations
    marker_messages = tuple(
        message.model_dump(mode="json")
        for message in envelope.compression_generations[0].messages
    )
    assert extract_marker_hashes(marker_messages) == (MARKER_HASH,)

    ccr_model = CapturingModel(
        (
            tool_call("ccr", "headroom_retrieve", {"hash": MARKER_HASH}),
            {"content": "answered from EXACT CCR ORIGINAL", "tool_calls": []},
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack["app"]), base_url="http://memory"
    ) as http:
        agent = AgentChatClient(
            memory_api_url="http://memory", model_call=ccr_model, http_client=http
        )
        assert "EXACT CCR ORIGINAL" in await agent.turn("u", "old", "recall exact")
    assert ccr_model.calls[1]["messages"][-1]["content"] == "EXACT CCR ORIGINAL"

    stack["recall"].fail = True
    fallback_model = CapturingModel(
        (
            tool_call("ccr-miss", "headroom_retrieve", {"hash": MARKER_HASH}),
            tool_call(
                "grep",
                "Grep",
                {
                    "path": "journal://current-session",
                    "pattern": "TTL is 43200",
                    "output_mode": "content",
                },
            ),
            tool_call(
                "read",
                "Read",
                {
                    "file_path": "journal://current-session",
                    "offset": 180,
                    "limit": 1,
                },
            ),
            {"content": "TTL is 43200", "tool_calls": []},
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack["app"]), base_url="http://memory"
    ) as http:
        agent = AgentChatClient(
            memory_api_url="http://memory", model_call=fallback_model, http_client=http
        )
        assert await agent.turn("u", "old", "TTL exact?") == "TTL is 43200"
    assert "TTL is 43200" in fallback_model.calls[2]["messages"][-1]["content"]
    assert "TTL is 43200" in fallback_model.calls[3]["messages"][-1]["content"]

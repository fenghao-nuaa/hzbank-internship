from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from short_term_memory.compression.auto_compact import AutoCompactContext, ModelProfile
from short_term_memory.compression.continuity_model import CompactionModelResponse
from short_term_memory.compression.context_query import load_active_messages
from short_term_memory.compression.traditional_compact import (
    TraditionalCompactContext,
    compact_conversation,
)
from short_term_memory.models import (
    AutoCompactTrackingState,
    CompressionGeneration,
    MemoryEvent,
    MemorySummaryEnvelope,
    SessionCompressionMessage,
)
from short_term_memory.service.context_coordinator import ContextCoordinator
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
PROFILE = ModelProfile(context_window_tokens=200_000, max_output_tokens=20_000)


def event(sequence: int, content: str) -> MemoryEvent:
    return MemoryEvent(
        sequence=sequence,
        event_id=f"event-{sequence}",
        role="user",
        content_type="conversation",
        content=content,
        metadata={},
        sha256=sha256(content.encode()).hexdigest(),
        created_at=NOW.isoformat(),
    )


def generation(number: int, start: int, through: int, content: str):
    return CompressionGeneration(
        generation=number,
        from_sequence=start,
        through_sequence=through,
        messages=(SessionCompressionMessage(role="assistant", content=content),),
        tokens_before=100_000,
        tokens_after=20_000,
        created_at=NOW.isoformat(),
        ccr_expires_at=(NOW + timedelta(hours=1)).isoformat(),
    )


class Store:
    def __init__(self, envelope: MemorySummaryEnvelope) -> None:
        self.envelope = envelope
        self.events: tuple[MemoryEvent, ...] = ()
        self.locked = False

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
        if self.envelope.version != expected_version:
            return False
        self.envelope = envelope
        return True


class ThresholdEstimator:
    def estimate(self, messages) -> int:
        if (
            len(messages) == 2
            and messages[0].get("compact_boundary") is not None
            and messages[1].get("is_compact_summary") is True
        ):
            return 1_000
        return 180_000


class ScriptedContinuityModel:
    def __init__(self) -> None:
        self.outputs = iter(("AB", "ABCD", "ABCDE"))
        self.calls: list[tuple[dict, ...]] = []

    async def compact(self, **kwargs):
        self.calls.append(tuple(kwargs["messages"]))
        summary = next(self.outputs)
        return CompactionModelResponse(
            content=f"<analysis>continuity</analysis><summary>{summary}</summary>",
            input_tokens=100,
            output_tokens=10,
        )


def message_texts(messages: tuple[dict, ...]) -> tuple[str, ...]:
    return tuple(str(message.get("content", "")) for message in messages)


@pytest.mark.asyncio
async def test_context_compacts_ab_to_abcd_to_abcde_and_hides_late_generation(
    tmp_path,
) -> None:
    originals = (event(1, "ORIGINAL A exact-7391"), event(2, "ORIGINAL B"),
                 event(3, "ORIGINAL C"), event(4, "ORIGINAL D"), event(5, "ORIGINAL E"))
    journals = JournalStore(VFSAdapter(tmp_path))
    for item in originals:
        journals.append_event("u", "s", item)

    store = Store(
        MemorySummaryEnvelope(
            version=1,
            compressed_through_sequence=2,
            compression_generations=(generation(1, 1, 2, "generation AB"),),
            auto_compact_tracking=AutoCompactTrackingState(),
            updated_at=NOW.isoformat(),
        )
    )
    estimator = ThresholdEstimator()
    model = ScriptedContinuityModel()

    def factory(profile, query_source, session_memory):
        del session_memory

        async def l4(messages, threshold):
            del messages, threshold
            return None

        async def l3(messages, tracking):
            del tracking
            return await compact_conversation(
                messages,
                TraditionalCompactContext(
                    model=model,
                    model_name="compact-model",
                    token_estimator=estimator,
                    clock=lambda: NOW,
                ),
                is_auto_compact=True,
            )

        return AutoCompactContext(
            model_profile=profile,
            token_estimator=estimator,
            query_source=query_source,
            try_session_memory=l4,
            compact_conversation=l3,
        )

    coordinator = ContextCoordinator(
        store=store,
        checkpoint_journal=type(
            "CheckpointJournal",
            (),
            {"append_compaction_checkpoint": lambda self, *args: None},
        )(),
        token_estimator=estimator,
        auto_context_factory=factory,
        history_turns=10,
        headroom_proxy_url="http://headroom/v1",
        scope_headers_factory=lambda user, session: {"scope": f"{user}:{session}"},
        clock=lambda: NOW,
    )

    first = await coordinator.prepare(user_id="u", session_id="s", model_profile=PROFILE)
    assert "AB" in str(first.messages[1].content)

    store.envelope = store.envelope.model_copy(
        update={
            "version": store.envelope.version + 1,
            "compressed_through_sequence": 4,
            "compression_generations": (
                *store.envelope.compression_generations,
                generation(2, 3, 4, "generation CD"),
            ),
        }
    )
    second = await coordinator.prepare(user_id="u", session_id="s", model_profile=PROFILE)
    second_inputs = message_texts(model.calls[-1])
    assert any("AB" in text for text in second_inputs)
    assert "generation CD" in second_inputs
    assert "generation AB" not in second_inputs
    assert "ABCD" in str(second.messages[1].content)

    store.events = (originals[4],)
    third = await coordinator.prepare(user_id="u", session_id="s", model_profile=PROFILE)
    third_inputs = message_texts(model.calls[-1])
    assert any("ABCD" in text for text in third_inputs)
    assert "ORIGINAL E" in third_inputs
    assert "generation CD" not in third_inputs
    assert "ABCDE" in str(third.messages[1].content)

    store.envelope = store.envelope.model_copy(
        update={
            "version": store.envelope.version + 1,
            "compressed_through_sequence": 5,
            "compression_generations": (
                *store.envelope.compression_generations,
                generation(3, 5, 5, "late generation E"),
            ),
        }
    )
    final_messages = load_active_messages(store.envelope, store.events, NOW)
    assert "late generation E" not in [str(item.content) for item in final_messages]
    assert estimator.estimate(tuple(item.model_dump(mode="json") for item in third.messages)) < 167_000
    assert tuple(item.content for item in journals.read_original_range("u", "s", 1, 5)) == tuple(
        item.content for item in originals
    )

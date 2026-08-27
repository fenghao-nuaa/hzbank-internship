import json
from pathlib import Path

from dream.core.events import TaskCompletedEvent
from dream.extraction.cache import ReviewStageCache
from dream.extraction.models import ReviewEventDisposition, ReviewResult
from dream.core.scope import ScopeIds
from dream.memory.storage.snapshots import ContextSnapshot


IDS = ScopeIds("acme", "assistant", "alice")


class CacheableBackend:
    validated_semantic_cache = True
    model = "agnes-v1"
    structured_mode = "tools"
    prompt_version = "combined-review-v2"
    max_completion_tokens = 2000


def inputs():
    event = TaskCompletedEvent(
        event_id="evt-1",
        task_id="task-1",
        scope=IDS,
        completed_at="2026-07-21T10:00:00+08:00",
        interrupted=False,
        tool_iterations=1,
        transcript=({"role": "user", "content": "remember this"},),
        final_response="done",
        source_refs=(),
    )
    snapshot = ContextSnapshot("snapshot-1", IDS, {}, "2026-07-21T02:00:00Z")
    allowed = {event.event_id: frozenset({"memory_manage"})}
    return (event,), snapshot, allowed


def test_cache_key_changes_with_model_identity(tmp_path: Path) -> None:
    cache = ReviewStageCache(tmp_path)
    events, snapshot, allowed = inputs()
    first = CacheableBackend()
    second = CacheableBackend()
    second.model = "agnes-v2"

    first_key = cache.key_for(IDS, events, allowed, snapshot, first)
    second_key = cache.key_for(IDS, events, allowed, snapshot, second)

    assert first_key is not None
    assert second_key is not None
    assert first_key.input_hash != second_key.input_hash


def test_cache_rejects_a_locally_modified_validated_result(tmp_path: Path) -> None:
    cache = ReviewStageCache(tmp_path)
    events, snapshot, allowed = inputs()
    key = cache.key_for(IDS, events, allowed, snapshot, CacheableBackend())
    assert key is not None
    cache.store(key, ReviewResult(actions=(), summary="validated"))
    path = tmp_path / "review-cache" / f"{key.input_hash}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"]["summary"] = "modified after validation"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.load(key) is None


def test_cache_round_trip_preserves_event_dispositions(tmp_path: Path) -> None:
    cache = ReviewStageCache(tmp_path)
    events, snapshot, allowed = inputs()
    key = cache.key_for(IDS, events, allowed, snapshot, CacheableBackend())
    assert key is not None
    result = ReviewResult(
        actions=(),
        summary="validated",
        event_dispositions=(
            ReviewEventDisposition(
                event_id="evt-1",
                disposition="no_durable_signal",
                reason="No durable signal.",
            ),
        ),
    )

    cache.store(key, result)
    restored = cache.load(key)

    assert restored is not None
    assert restored.event_dispositions == result.event_dispositions

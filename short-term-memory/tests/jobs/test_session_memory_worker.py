from datetime import datetime, timezone
from hashlib import sha256

import pytest

from short_term_memory.compression.session_memory_prompt import EMPTY_SESSION_MEMORY
from short_term_memory.jobs.session_memory_queue import (
    SessionMemoryJob,
    SessionMemoryJobLease,
)
from short_term_memory.jobs.session_memory_worker import SessionMemoryWorker
from short_term_memory.models import MemoryEvent, MemorySummaryEnvelope
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.storage.fake_redis import AsyncFakeRedis


NOW = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)


def populated_memory() -> str:
    lines: list[str] = []
    for line in EMPTY_SESSION_MEMORY.splitlines():
        lines.append(line)
        if line.startswith("_") and line.endswith("_"):
            lines.append("Concrete continuity detail.")
    return "\n".join(lines) + "\n"


def event(sequence: int, role: str, content: str) -> MemoryEvent:
    return MemoryEvent(
        sequence=sequence,
        event_id=f"event-{sequence}",
        role=role,
        content_type="conversation",
        content=content,
        metadata={},
        sha256=sha256(content.encode()).hexdigest(),
        created_at=NOW.isoformat(),
    )


class FakeQueue:
    def __init__(self, job: SessionMemoryJob) -> None:
        self.lease_value = SessionMemoryJobLease(job=job, token="queue-token")
        self.acked = False
        self.retried = False

    async def lease(self, token: str, *, now_unix_ms: int):
        del token, now_unix_ms
        value, self.lease_value = self.lease_value, None
        return value

    async def ack(self, lease: SessionMemoryJobLease) -> bool:
        del lease
        self.acked = True
        return True

    async def retry(self, lease: SessionMemoryJobLease, *, now_unix_ms: int) -> str:
        del lease, now_unix_ms
        self.retried = True
        return "retry"


class RecordingModel:
    def __init__(self, output: str) -> None:
        self.output = output
        self.update_calls: list[dict[str, object]] = []

    async def update_session_memory(self, **kwargs: object) -> str:
        self.update_calls.append(kwargs)
        return self.output


async def build_worker(tmp_path, *, model_output: str, version: int = 1):
    redis = AsyncFakeRedis()
    store = AsyncRedisMemoryStore(redis, ttl_seconds=3600)
    journals = JournalStore(VFSAdapter(tmp_path))
    for item in (
        event(1, "user", "old question"),
        event(2, "assistant", "old answer"),
        event(3, "user", "new question"),
        event(4, "assistant", "new answer"),
    ):
        journals.append_event("u", "s", item)
    envelope = MemorySummaryEnvelope(
        version=version,
        compressed_through_sequence=0,
        updated_at=NOW.isoformat(),
    )
    assert await store.compare_and_set_envelope("u", "s", 0, envelope)
    job = SessionMemoryJob(
        user_id="u", session_id="s", expected_version=version,
        requested_through_sequence=4,
    )
    queue = FakeQueue(job)
    model = RecordingModel(model_output)
    worker = SessionMemoryWorker(
        queue=queue,
        store=store,
        journals=journals,
        continuity_model=model,
        model_name="claude-sonnet",
        clock=lambda: NOW,
    )
    return worker, queue, store, model, redis


@pytest.mark.asyncio
async def test_worker_updates_memory_and_coverage_only_after_valid_output(tmp_path) -> None:
    worker, queue, store, model, _ = await build_worker(
        tmp_path, model_output=populated_memory()
    )
    result = await worker.run_once()
    saved = await store.read_envelope("u", "s")

    assert result.state == "acked"
    assert queue.acked
    assert saved is not None and saved.version == 2
    assert saved.session_memory is not None
    assert saved.session_memory.content == populated_memory()
    assert saved.session_memory.covered_through_sequence == 4
    assert model.update_calls[0]["messages"] == (
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    )
    assert model.update_calls[0]["query_source"] == "session_memory"
    checkpoint = worker.journals.read_latest_compaction_checkpoint("u", "s")
    assert checkpoint is not None
    assert checkpoint.session_memory == saved.session_memory


@pytest.mark.asyncio
async def test_invalid_output_does_not_advance_coverage(tmp_path) -> None:
    worker, queue, store, _, _ = await build_worker(
        tmp_path, model_output="# Session Title\ninvalid"
    )
    result = await worker.run_once()
    saved = await store.read_envelope("u", "s")

    assert result.state == "retry"
    assert queue.retried
    assert saved is not None and saved.version == 1
    assert saved.session_memory is None


@pytest.mark.asyncio
async def test_newer_headroom_envelope_is_stale_and_never_overwritten(tmp_path) -> None:
    worker, queue, store, _, _ = await build_worker(
        tmp_path, model_output=populated_memory(), version=2
    )
    current = await store.read_envelope("u", "s")
    assert current is not None
    newer = current.model_copy(update={"version": 3, "updated_at": NOW.isoformat()})
    assert await store.compare_and_set_envelope("u", "s", 2, newer)

    result = await worker.run_once()
    saved = await store.read_envelope("u", "s")

    assert result.state == "stale"
    assert queue.acked
    assert saved is not None and saved.version == 3
    assert saved.session_memory is None


@pytest.mark.asyncio
async def test_cas_conflict_after_model_update_returns_stale_without_advancing(tmp_path) -> None:
    worker, queue, store, model, _ = await build_worker(
        tmp_path, model_output=populated_memory()
    )
    original_cas = store.compare_and_set_envelope

    async def conflicting_cas(user_id, session_id, expected_version, envelope):
        current = await store.read_envelope(user_id, session_id)
        assert current is not None
        headroom_update = current.model_copy(
            update={"version": current.version + 1, "updated_at": NOW.isoformat()}
        )
        assert await original_cas(
            user_id, session_id, expected_version, headroom_update
        )
        return await original_cas(user_id, session_id, expected_version, envelope)

    store.compare_and_set_envelope = conflicting_cas
    result = await worker.run_once()
    saved = await store.read_envelope("u", "s")

    assert model.update_calls
    assert result.state == "stale"
    assert queue.acked
    assert saved is not None and saved.version == 2
    assert saved.session_memory is None
    assert worker.journals.read_latest_compaction_checkpoint("u", "s") is None

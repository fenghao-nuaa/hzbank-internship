from pathlib import Path

from dream.core.events import TaskCompletedEvent
from dream.application.progress import ReviewProgressStore
from dream.core.scope import ScopeIds
from dream.application.service import DreamService


def event(
    event_id: str,
    *,
    user: str = "alice",
    text: str = "ordinary completed conversation",
) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=ScopeIds("acme", "assistant", user),
        completed_at="2026-07-17T10:00:00+08:00",
        interrupted=False,
        tool_iterations=10,
        transcript=(
            {"role": "user", "content": text},
            {"role": "assistant", "content": "Understood."},
        ),
        final_response="Understood.",
        source_refs=(),
    )


def test_review_progress_is_append_only_and_idempotent(tmp_path: Path) -> None:
    store = ReviewProgressStore(tmp_path / "reviewed.jsonl")

    store.append("evt-1")
    store.append("evt-1")
    store.append("evt-2")

    assert store.read_all() == ("evt-1", "evt-2")
    assert store.contains("evt-2") is True


def test_restart_recovers_only_unprocessed_events(tmp_path: Path) -> None:
    first = DreamService(tmp_path)
    first.ingest_conversation(event("evt-1"))
    first.run_pending()
    first.ingest_conversation(event("evt-2"))

    restarted = DreamService(tmp_path)

    assert restarted.scheduler.pending_event_ids() == ("evt-2",)


def test_successful_no_change_review_is_still_durable(tmp_path: Path) -> None:
    service = DreamService(tmp_path)
    service.ingest_conversation(event("evt-no-change"))

    result = service.run_pending()

    assert result[0]["status"] == "success"
    assert result[0]["source_event_ids"] == ["evt-no-change"]
    assert service.review_progress.contains("evt-no-change")


def test_scoped_run_leaves_other_user_pending(tmp_path: Path) -> None:
    service = DreamService(tmp_path)
    service.ingest_conversation(event("evt-alice", user="alice"))
    service.ingest_conversation(event("evt-bob", user="bob"))

    runs = service.run_pending(ScopeIds("acme", "assistant", "alice"))

    assert [run["source_event_ids"] for run in runs] == [["evt-alice"]]
    assert service.scheduler.pending_event_ids() == ("evt-bob",)


def test_failed_review_remains_recoverable(tmp_path: Path) -> None:
    class FailingBackend:
        def review(self, request):
            raise RuntimeError("provider unavailable")

    service = DreamService(tmp_path, backend=FailingBackend())
    service.ingest_conversation(event("evt-failed"))

    runs = service.run_pending()

    assert runs[0]["status"] == "failed"
    assert service.review_progress.contains("evt-failed") is False
    restarted = DreamService(tmp_path)
    assert restarted.scheduler.pending_event_ids() == ("evt-failed",)

from datetime import datetime, timezone
from pathlib import Path

from dream.core.events import TaskCompletedEvent
from dream.extraction.models import ReviewResult
from dream.application.scheduler import ReviewSchedulePolicy
from dream.core.scope import ScopeIds
from dream.application.service import DreamService


def _event(
    event_id: str,
    *,
    user_id: str = "alice",
    completed_at: str = "2026-07-21T10:00:00+08:00",
) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=ScopeIds("acme", "assistant", user_id),
        completed_at=completed_at,
        interrupted=False,
        tool_iterations=1,
        transcript=(
            {"role": "user", "content": "ordinary completed conversation"},
            {"role": "assistant", "content": "Understood."},
        ),
        final_response="Understood.",
        source_refs=(),
    )


class RecordingBackend:
    def __init__(self) -> None:
        self.event_ids: list[str] = []

    def review(self, request):
        self.event_ids.append(request.event_id)
        return ReviewResult(actions=(), summary="Nothing to save.")


def test_automatic_review_runs_only_scopes_due_under_adaptive_policy(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    service = DreamService(
        tmp_path,
        backend=backend,
        review_schedule=ReviewSchedulePolicy(),
    )
    service.ingest_conversation(_event("evt-alice"))
    service.ingest_conversation(
        _event(
            "evt-bob",
            user_id="bob",
            completed_at="2026-07-21T11:30:00+08:00",
        )
    )

    runs = service.run_due_pending(datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc))

    assert [run["source_event_ids"] for run in runs] == [["evt-alice"]]
    assert backend.event_ids == ["evt-alice"]
    assert service.scheduler.pending_event_ids() == ("evt-bob",)


def test_manual_review_still_runs_immediately_before_adaptive_threshold(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    service = DreamService(tmp_path, backend=backend)
    service.ingest_conversation(_event("evt-manual"))

    runs = service.run_pending(ScopeIds("acme", "assistant", "alice"))

    assert [run["source_event_ids"] for run in runs] == [["evt-manual"]]
    assert backend.event_ids == ["evt-manual"]


class BatchRecordingBackend:
    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    def review_batch(self, request):
        self.batches.append(tuple(event.event_id for event in request.events))
        return ReviewResult(actions=(), summary="Nothing to save.")


def test_same_user_pending_events_use_one_batch_backend_call(tmp_path: Path) -> None:
    backend = BatchRecordingBackend()
    service = DreamService(tmp_path, backend=backend)
    service.ingest_conversation(_event("evt-batch-1"))
    service.ingest_conversation(
        _event("evt-batch-2", completed_at="2026-07-21T10:01:00+08:00")
    )

    runs = service.run_pending(ScopeIds("acme", "assistant", "alice"))

    assert backend.batches == [("evt-batch-1", "evt-batch-2")]
    assert len(runs) == 1
    assert runs[0]["source_event_ids"] == ["evt-batch-1", "evt-batch-2"]
    assert service.review_progress.contains("evt-batch-1")
    assert service.review_progress.contains("evt-batch-2")


def test_manual_all_scope_run_never_mixes_users_in_one_batch(tmp_path: Path) -> None:
    backend = BatchRecordingBackend()
    service = DreamService(tmp_path, backend=backend)
    service.ingest_conversation(_event("evt-alice"))
    service.ingest_conversation(_event("evt-bob", user_id="bob"))

    service.run_pending()

    assert backend.batches == [("evt-alice",), ("evt-bob",)]


def test_failed_batch_remains_pending_without_same_iteration_retry(
    tmp_path: Path,
) -> None:
    class FailingBatchBackend:
        def __init__(self) -> None:
            self.calls = 0

        def review_batch(self, request):
            self.calls += 1
            return ReviewResult(
                actions=(),
                summary="Invalid after repair.",
                status="failed",
                error="invalid structured output",
            )

    backend = FailingBatchBackend()
    service = DreamService(tmp_path, backend=backend)
    service.ingest_conversation(_event("evt-failed-1"))
    service.ingest_conversation(
        _event("evt-failed-2", completed_at="2026-07-21T10:01:00+08:00")
    )

    runs = service.run_due_pending(datetime(2026, 7, 21, 4, 1, tzinfo=timezone.utc))

    assert backend.calls == 1
    assert [run["status"] for run in runs] == ["failed"]
    assert service.scheduler.pending_event_ids() == (
        "evt-failed-1",
        "evt-failed-2",
    )
    assert not service.review_progress.contains("evt-failed-1")
    assert not service.review_progress.contains("evt-failed-2")

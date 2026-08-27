from datetime import datetime, timedelta, timezone

from dream.curators.registry import CuratorRegistry
from dream.core.events import TaskCompletedEvent
from dream.application.scheduler import DreamScheduler, ReviewSchedulePolicy
from dream.core.scope import ScopeIds


def _event(
    event_id: str,
    *,
    interrupted: bool = False,
    completed_at: str = "2026-07-15T10:00:00+08:00",
    user_id: str = "alice",
    content: str = "I prefer concise answers",
) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=ScopeIds("acme", "assistant", user_id),
        completed_at=completed_at,
        interrupted=interrupted,
        tool_iterations=12,
        transcript=({"role": "user", "content": content},),
        final_response="Understood." if not interrupted else "",
        source_refs=(),
    )


def test_scheduler_ignores_interrupted_conversations() -> None:
    scheduler = DreamScheduler(review_threshold=10)
    scheduler.enqueue(_event("evt-interrupted", interrupted=True))
    scheduler.enqueue(_event("evt-completed"))
    assert scheduler.pending_event_ids() == ("evt-completed",)


def test_scheduler_pops_only_the_requested_scope() -> None:
    scheduler = DreamScheduler(review_threshold=10)
    alice = _event("evt-alice")
    bob = TaskCompletedEvent(
        **{
            **alice.__dict__,
            "event_id": "evt-bob",
            "scope": ScopeIds("acme", "assistant", "bob"),
        }
    )
    scheduler.enqueue(alice)
    scheduler.enqueue(bob)

    assert scheduler.pop_pending(bob.scope) == bob
    assert scheduler.pending_event_ids() == ("evt-alice",)


def test_scheduler_does_not_enqueue_the_same_pending_event_twice() -> None:
    scheduler = DreamScheduler(review_threshold=10)
    completed = _event("evt-completed")

    scheduler.enqueue_unless_pending(completed)
    scheduler.enqueue_unless_pending(completed)

    assert scheduler.pending_event_ids() == ("evt-completed",)


def test_low_volume_scope_becomes_due_only_after_two_hours_idle() -> None:
    scheduler = DreamScheduler()
    scheduler.enqueue(_event("evt-idle"))

    assert (
        scheduler.pop_due_batch(datetime(2026, 7, 15, 3, 59, tzinfo=timezone.utc))
        is None
    )

    batch = scheduler.pop_due_batch(datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc))
    assert batch is not None
    assert batch.scope.user_id == "alice"
    assert batch.trigger_reasons == ("idle",)
    assert tuple(event.event_id for event in batch.events) == ("evt-idle",)


def test_pending_token_threshold_triggers_without_waiting_for_idle() -> None:
    scheduler = DreamScheduler(
        policy=ReviewSchedulePolicy(max_batch_tokens=10),
        token_estimator=lambda event: 6,
    )
    scheduler.enqueue(_event("evt-token-1"))
    scheduler.enqueue(_event("evt-token-2"))

    batch = scheduler.pop_due_batch(datetime(2026, 7, 15, 2, 1, tzinfo=timezone.utc))

    assert batch is not None
    assert "tokens" in batch.trigger_reasons
    assert batch.estimated_tokens == 6
    assert tuple(event.event_id for event in batch.events) == ("evt-token-1",)
    assert scheduler.pending_event_ids() == ("evt-token-2",)


def test_pending_event_threshold_triggers_and_caps_the_batch() -> None:
    scheduler = DreamScheduler(
        policy=ReviewSchedulePolicy(max_batch_events=2, max_batch_tokens=1000),
        token_estimator=lambda event: 1,
    )
    for number in range(3):
        scheduler.enqueue(_event(f"evt-count-{number}"))

    batch = scheduler.pop_due_batch(datetime(2026, 7, 15, 2, 1, tzinfo=timezone.utc))

    assert batch is not None
    assert "events" in batch.trigger_reasons
    assert tuple(event.event_id for event in batch.events) == (
        "evt-count-0",
        "evt-count-1",
    )
    assert scheduler.pending_event_ids() == ("evt-count-2",)


def test_oldest_pending_event_triggers_after_maximum_wait() -> None:
    scheduler = DreamScheduler(
        policy=ReviewSchedulePolicy(
            idle_after=timedelta(hours=48),
            max_wait=timedelta(hours=24),
        )
    )
    scheduler.enqueue(_event("evt-oldest"))
    scheduler.enqueue(_event("evt-recent", completed_at="2026-07-16T09:30:00+08:00"))

    batch = scheduler.pop_due_batch(datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc))

    assert batch is not None
    assert batch.trigger_reasons == ("max_wait",)


def test_activity_and_batches_are_isolated_per_user_scope() -> None:
    scheduler = DreamScheduler()
    scheduler.enqueue(_event("evt-alice", user_id="alice"))
    scheduler.enqueue(
        _event(
            "evt-bob",
            user_id="bob",
            completed_at="2026-07-15T11:30:00+08:00",
        )
    )

    batch = scheduler.pop_due_batch(datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc))

    assert batch is not None
    assert batch.scope.user_id == "alice"
    assert tuple(event.event_id for event in batch.events) == ("evt-alice",)
    assert scheduler.pending_event_ids() == ("evt-bob",)


def test_due_batch_is_split_in_event_completion_order() -> None:
    scheduler = DreamScheduler(
        policy=ReviewSchedulePolicy(max_batch_events=2, max_batch_tokens=1000),
        token_estimator=lambda event: 1,
    )
    scheduler.enqueue(_event("evt-late", completed_at="2026-07-15T10:02:00+08:00"))
    scheduler.enqueue(_event("evt-early", completed_at="2026-07-15T10:00:00+08:00"))
    scheduler.enqueue(_event("evt-middle", completed_at="2026-07-15T10:01:00+08:00"))

    batch = scheduler.pop_due_batch(datetime(2026, 7, 15, 2, 3, tzinfo=timezone.utc))

    assert batch is not None
    assert tuple(event.event_id for event in batch.events) == (
        "evt-early",
        "evt-middle",
    )
    assert scheduler.pending_event_ids() == ("evt-late",)


def test_curator_registry_runs_only_due_curators() -> None:
    class RecordingCurator:
        name = "recording"

        def __init__(self) -> None:
            self.ran = False

        def should_run(self, now: datetime) -> bool:
            return True

        def run(self) -> str:
            self.ran = True
            return "ok"

    curator = RecordingCurator()
    results = CuratorRegistry([curator]).run_due(datetime.now(timezone.utc))
    assert curator.ran is True
    assert results == {"recording": "ok"}

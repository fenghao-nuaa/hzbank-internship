from dream.core.events import TaskCompletedEvent
from dream.extraction.backend import DeterministicReviewBackend
from dream.extraction.models import ArtifactKind, ReviewRequest
from dream.application.review_orchestrator import BackgroundReviewOrchestrator
from dream.core.scope import ScopeIds


def test_review_routes_preference_without_exposing_unrelated_tools() -> None:
    backend = DeterministicReviewBackend()
    result = backend.review(
        ReviewRequest(
            event_id="evt-1",
            transcript_text="User: I prefer concise answers",
            final_response="Understood.",
            allowed_tools=frozenset({"memory_manage"}),
        )
    )
    assert result.actions[0].kind is ArtifactKind.USER_PROFILE
    assert result.actions[0].tool_name == "memory_manage"
    assert {action.tool_name for action in result.actions} <= {"memory_manage"}


def test_interrupted_event_is_not_reviewed() -> None:
    event = TaskCompletedEvent(
        event_id="evt-2",
        task_id="task-2",
        scope=ScopeIds("acme", "assistant", "alice"),
        completed_at="2026-07-15T10:00:00+08:00",
        interrupted=True,
        tool_iterations=12,
        transcript=({"role": "user", "content": "I prefer concise answers"},),
        final_response="",
        source_refs=(),
    )
    result = BackgroundReviewOrchestrator(DeterministicReviewBackend()).review(
        event, allowed_tools=frozenset({"memory_manage"})
    )
    assert result.status == "skipped"
    assert result.actions == ()


def test_review_can_emit_both_priority_artifacts() -> None:
    result = DeterministicReviewBackend().review(
        ReviewRequest(
            event_id="evt-3",
            transcript_text=(
                "User: I prefer concise answers\n"
                "Assistant decision: verify before risky action because it is irreversible"
            ),
            final_response="Verified before applying the change.",
            allowed_tools=frozenset({"memory_manage", "decision_card_manage"}),
        )
    )
    assert {action.kind for action in result.actions} == {
        ArtifactKind.USER_PROFILE,
        ArtifactKind.DECISION_CARD,
    }

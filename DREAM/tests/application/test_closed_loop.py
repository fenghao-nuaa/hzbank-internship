from pathlib import Path
import json
import time

import pytest

from dream.application.closed_loop import (
    ClosedLoopCoordinator,
    ClosedLoopError,
    TaskStartBlocked,
)
from dream.core.events import TaskCompletedEvent
from dream.governance.policy import (
    AutoWritebackDecision,
    GovernanceMode,
    RiskLevel,
)
from dream.memory.publication import PublicationStatus, PublicationTransitionError
from dream.extraction.models import (
    ArtifactKind,
    ReviewAction,
    ReviewEventDisposition,
    ReviewResult,
)
from dream.core.scope import ScopeIds
from dream.application.service import DreamService
from dream.memory.writeback import DeterministicWritebackBackend


IDS = ScopeIds("dream-lab", "enterprise-colleague", "python-beginner")


class RequireReviewPolicy:
    def decide_all(self, artifacts) -> AutoWritebackDecision:
        return AutoWritebackDecision(
            mode=GovernanceMode.REQUIRE_REVIEW,
            risk_level=RiskLevel.HIGH,
            reason="manual publication compatibility test",
        )


def event(event_id: str, ids: ScopeIds = IDS) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=ids,
        completed_at="2026-07-17T10:00:00+08:00",
        interrupted=False,
        tool_iterations=10,
        transcript=(
            {"role": "user", "content": "I prefer concise answers"},
            {"role": "assistant", "content": "Always verify before risky action"},
        ),
        final_response="Verified before applying the change.",
        source_refs=(),
    )


def coordinator(tmp_path: Path, *, backend=None):
    service = DreamService(tmp_path)
    closed_loop = ClosedLoopCoordinator(
        service,
        writeback_backend=backend or DeterministicWritebackBackend(),
        governance_policy=RequireReviewPolicy(),
    )
    return closed_loop, service


def activate(closed_loop: ClosedLoopCoordinator, version: int):
    closed_loop.approve(IDS, version)
    closed_loop.confirm_writeback(
        IDS,
        version,
        character_written=True,
        user_written=True,
    )
    return closed_loop.activate(IDS, version)


def test_next_task_waits_for_latest_event_to_be_active(tmp_path: Path) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-1"))

    with pytest.raises(TaskStartBlocked, match="evt-1"):
        closed_loop.assert_task_can_start(IDS)

    candidate = closed_loop.dream(IDS)
    active = activate(closed_loop, candidate.version)

    assert active.processed_through_event_id == "evt-1"
    closed_loop.assert_task_can_start(IDS)


def test_failed_writeback_restores_previous_snapshot(tmp_path: Path) -> None:
    class FailingBackend(DeterministicWritebackBackend):
        def render_user_persona(self, user_profile: str, limit: int) -> str:
            raise RuntimeError("provider unavailable")

    closed_loop, service = coordinator(tmp_path, backend=FailingBackend())
    service.ingest_conversation(event("evt-failed"))

    with pytest.raises(ClosedLoopError):
        closed_loop.dream(IDS)

    latest = closed_loop.status(IDS)["latest"]
    assert latest.status is PublicationStatus.FAILED
    assert closed_loop.status(IDS)["active"] is None
    assert closed_loop.publications(IDS).pending_event_ids() == ("evt-failed",)
    report_path = (
        tmp_path
        / "tenants/dream-lab/agents/enterprise-colleague/dream-reports"
        / "publication-000001-failed.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["transaction_rolled_back"] is True


def test_failed_candidate_can_be_retried_from_its_source_event(tmp_path: Path) -> None:
    class FailOnceBackend(DeterministicWritebackBackend):
        def __init__(self) -> None:
            self.failed = False

        def render_user_persona(self, user_profile: str, limit: int) -> str:
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary provider failure")
            return super().render_user_persona(user_profile, limit)

    backend = FailOnceBackend()
    closed_loop, service = coordinator(tmp_path, backend=backend)
    service.ingest_conversation(event("evt-retry"))
    with pytest.raises(ClosedLoopError):
        closed_loop.dream(IDS)

    candidate = closed_loop.dream(IDS)

    assert candidate.status is PublicationStatus.READY_FOR_REVIEW
    assert candidate.processed_through_event_id == "evt-retry"


def test_validated_semantic_result_is_reused_after_local_writeback_failure(
    tmp_path: Path,
) -> None:
    class CountingSemanticBackend:
        validated_semantic_cache = True
        model = "agnes-test"
        structured_mode = "tools"
        prompt_version = "combined-review-v2"

        def __init__(self) -> None:
            self.calls = 0

        def review_batch(self, request):
            self.calls += 1
            event_id = request.events[0].event_id
            return ReviewResult(
                actions=(
                    ReviewAction(
                        kind=ArtifactKind.USER_PROFILE,
                        tool_name="memory_manage",
                        payload={
                            "action": "add",
                            "target": "user",
                            "content": "Prefers concise answers.",
                        },
                        source_event_id=event_id,
                    ),
                    ReviewAction(
                        kind=ArtifactKind.DECISION_CARD,
                        tool_name="decision_card_manage",
                        payload={
                            "id": "verify-before-risky-action",
                            "title": "Verify before risky action",
                            "scenario": "A risky action is requested.",
                            "signals": ["irreversible change"],
                            "principle": "Verify before applying it.",
                            "outcome": "Avoid unsafe changes.",
                            "boundaries": "Skip only for reversible operations.",
                            "confidence": 0.9,
                        },
                        source_event_id=event_id,
                    ),
                ),
                summary="validated semantic result",
                event_dispositions=(
                    ReviewEventDisposition(
                        event_id=event_id,
                        disposition="used",
                        reason=None,
                    ),
                ),
            )

    class FailOnceWriteback(DeterministicWritebackBackend):
        def __init__(self) -> None:
            self.calls = 0

        def render_user_persona(self, user_profile: str, limit: int) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("local writeback failed")
            return super().render_user_persona(user_profile, limit)

    semantic = CountingSemanticBackend()
    service = DreamService(tmp_path, backend=semantic)
    closed_loop = ClosedLoopCoordinator(
        service,
        writeback_backend=FailOnceWriteback(),
        governance_policy=RequireReviewPolicy(),
    )
    service.ingest_conversation(event("evt-cache"))

    with pytest.raises(ClosedLoopError):
        closed_loop.dream(IDS)
    candidate = closed_loop.dream(IDS)

    assert candidate.status is PublicationStatus.READY_FOR_REVIEW
    assert semantic.calls == 1
    cache_files = list((tmp_path / "review-cache").glob("*.json"))
    assert len(cache_files) == 1
    cache_payload = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert cache_payload["result"]["event_dispositions"] == [
        {"event_id": "evt-cache", "disposition": "used", "reason": None}
    ]
    report_files = list(
        (tmp_path / "tenants/dream-lab/agents/enterprise-colleague/dream-reports").glob(
            "review-*.json"
        )
    )
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in report_files]
    successful = [report for report in reports if report["status"] == "success"]
    assert successful[-1]["event_dispositions"] == [
        {"event_id": "evt-cache", "disposition": "used", "reason": None}
    ]


def test_overall_deadline_cancels_semantic_wait_and_restores_pending_event(
    tmp_path: Path,
) -> None:
    class BlockingBackend:
        model = "agnes-blocking"
        structured_mode = "tools"
        prompt_version = "combined-review-v2"

        def review_batch(self, request):
            time.sleep(1)
            return ReviewResult(actions=(), summary="too late")

    service = DreamService(tmp_path, backend=BlockingBackend())
    closed_loop = ClosedLoopCoordinator(
        service,
        writeback_backend=DeterministicWritebackBackend(),
        deadline_seconds=0.02,
    )
    service.ingest_conversation(event("evt-timeout"))
    started = time.monotonic()

    with pytest.raises(ClosedLoopError):
        closed_loop.dream(IDS)

    assert time.monotonic() - started < 0.5
    latest = closed_loop.status(IDS)["latest"]
    assert latest is not None
    assert latest.status is PublicationStatus.FAILED
    assert latest.failure_reason == "DreamDeadlineExceeded"
    assert closed_loop.publications(IDS).pending_event_ids() == ("evt-timeout",)
    assert not (
        tmp_path
        / "tenants/dream-lab/agents/enterprise-colleague/users/python-beginner/USER.md"
    ).exists()


def test_identical_writeback_hashes_do_not_require_repeated_paste(
    tmp_path: Path,
) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-1"))
    first = closed_loop.dream(IDS)
    activate(closed_loop, first.version)
    service.ingest_conversation(event("evt-2"))
    second = closed_loop.dream(IDS)
    closed_loop.approve(IDS, second.version)

    confirmed = closed_loop.confirm_writeback(
        IDS,
        second.version,
        character_written=False,
        user_written=False,
    )

    assert confirmed.character_definition_written is True
    assert confirmed.user_persona_written is True
    assert closed_loop.activate(IDS, second.version).status is PublicationStatus.ACTIVE


def test_rollback_restores_a_previous_active_version(tmp_path: Path) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-1"))
    first = closed_loop.dream(IDS)
    activate(closed_loop, first.version)
    service.ingest_conversation(event("evt-2"))
    second = closed_loop.dream(IDS)
    activate(closed_loop, second.version)

    restored = closed_loop.rollback(IDS, first.version)

    assert restored.version == first.version
    assert closed_loop.status(IDS)["active"].version == first.version


def test_reject_restores_input_state_and_requeues_source_event(
    tmp_path: Path,
) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-rejected"))
    candidate = closed_loop.dream(IDS)

    rejected = closed_loop.reject(IDS, candidate.version)

    assert rejected.status is PublicationStatus.FAILED
    assert closed_loop.publications(IDS).pending_event_ids() == ("evt-rejected",)
    assert service.scheduler.pending_event_ids() == ("evt-rejected",)
    report_path = (
        tmp_path
        / "tenants/dream-lab/agents/enterprise-colleague/dream-reports"
        / "publication-000001-failed.json"
    )
    assert (
        json.loads(report_path.read_text(encoding="utf-8"))["transaction_rolled_back"]
        is True
    )


def test_rejecting_active_version_does_not_restore_its_before_snapshot(
    tmp_path: Path,
) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-active"))
    candidate = closed_loop.dream(IDS)
    active = activate(closed_loop, candidate.version)
    profile_before = service.start_context(IDS)["user_profile"]

    with pytest.raises(PublicationTransitionError):
        closed_loop.reject(IDS, active.version)

    assert service.start_context(IDS)["user_profile"] == profile_before
    assert closed_loop.status(IDS)["active"].version == active.version

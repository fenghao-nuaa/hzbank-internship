from pathlib import Path

import pytest

from dream.memory.artifacts import AtomicArtifactStore
from dream.application.closed_loop import ClosedLoopCoordinator, ClosedLoopError
from dream.core.events import TaskCompletedEvent
from dream.memory.publication import PublicationStatus, PublicationTransitionError
from dream.extraction.models import ArtifactKind, ReviewAction, ReviewResult
from dream.core.scope import ScopeIds, resolve_scope
from dream.application.service import DreamService
from dream.memory.writeback import DeterministicWritebackBackend


IDS = ScopeIds("bank-lab", "governed-agent", "user-1")


def _event(event_id: str, text: str) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=IDS,
        completed_at="2026-07-22T10:00:00+08:00",
        interrupted=False,
        tool_iterations=1,
        transcript=(
            {"role": "user", "content": text},
            {"role": "assistant", "content": "Understood."},
        ),
        final_response="Understood.",
        source_refs=(),
    )


class StaticBackend:
    def __init__(self, actions: tuple[ReviewAction, ...]) -> None:
        self.actions = actions

    def review_batch(self, request) -> ReviewResult:
        return ReviewResult(actions=self.actions, summary="governance test")


class RepeatedPreferenceBackend:
    def review_batch(self, request) -> ReviewResult:
        event_id = request.events[-1].event_id
        return ReviewResult(
            actions=(
                ReviewAction(
                    kind=ArtifactKind.USER_PROFILE,
                    tool_name="memory_manage",
                    payload={
                        "action": "add",
                        "target": "user",
                        "content": "User prefers a separate immediate-action section.",
                        "confidence": 0.55,
                    },
                    source_event_id=event_id,
                ),
            ),
            summary="repeated preference",
        )


def _seed_decision_context(home: Path) -> None:
    paths = resolve_scope(home, IDS)
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(
        Path("DECISION_RULES.md"),
        "# AI Decision Rules\n\n- Verify first.\n  - Evidence cards: seed-card\n",
    )
    store.write_text(
        Path("CHARACTER_DEFINITION.md"), store.read_text(Path("DECISION_RULES.md"))
    )


def _seed_user_context(home: Path) -> None:
    paths = resolve_scope(home, IDS)
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(
        Path("users") / IDS.user_id / "USER.md",
        "User prefers verified information.\n<!-- dream-source: evt-seed -->\n",
    )


def _coordinator(home: Path, backend, *, writeback=None):
    service = DreamService(home, backend=backend)
    return (
        ClosedLoopCoordinator(
            service,
            writeback_backend=writeback or DeterministicWritebackBackend(),
        ),
        service,
    )


def test_low_risk_user_preference_auto_activates_without_approve(
    tmp_path: Path,
) -> None:
    _seed_decision_context(tmp_path)
    action = ReviewAction(
        kind=ArtifactKind.USER_PROFILE,
        tool_name="memory_manage",
        payload={
            "action": "add",
            "target": "user",
            "content": "User prefers answers that lead with the conclusion.",
            "confidence": 0.9,
        },
        source_event_id="evt-preference",
    )
    closed_loop, service = _coordinator(tmp_path, StaticBackend((action,)))
    service.ingest_conversation(_event("evt-preference", "以后回答问题先给结论。"))

    result = closed_loop.dream(IDS)

    assert result.status is PublicationStatus.ACTIVE
    assert closed_loop.status(IDS)["active"].version == result.version
    assert closed_loop.publications(IDS).pending_event_ids() == ()
    assert "lead with the conclusion" in service.start_context(IDS)["user_profile"]


def test_high_risk_transfer_permission_stays_ready_for_review(tmp_path: Path) -> None:
    _seed_decision_context(tmp_path)
    action = ReviewAction(
        kind=ArtifactKind.USER_PROFILE,
        tool_name="memory_manage",
        payload={
            "action": "add",
            "target": "user",
            "content": "User says future transfers do not need confirmation.",
            "confidence": 0.99,
        },
        source_event_id="evt-risk",
    )
    closed_loop, service = _coordinator(tmp_path, StaticBackend((action,)))
    service.ingest_conversation(_event("evt-risk", "以后转账不用确认。"))

    result = closed_loop.dream(IDS)

    assert result.status is PublicationStatus.READY_FOR_REVIEW
    assert closed_loop.status(IDS)["active"] is None
    assert closed_loop.publications(IDS).pending_event_ids() == ("evt-risk",)
    assert (
        "transfers do not need confirmation"
        not in service.start_context(IDS)["user_profile"]
    )

    with pytest.raises(PublicationTransitionError):
        closed_loop.activate(IDS, result.version)
    assert (
        "transfers do not need confirmation"
        not in service.start_context(IDS)["user_profile"]
    )

    closed_loop.approve(IDS, result.version)
    closed_loop.confirm_writeback(
        IDS,
        result.version,
        character_written=True,
        user_written=True,
    )
    closed_loop.activate(IDS, result.version)

    assert (
        "transfers do not need confirmation"
        in service.start_context(IDS)["user_profile"]
    )


def test_complete_decision_card_updates_rules_and_auto_activates(
    tmp_path: Path,
) -> None:
    _seed_user_context(tmp_path)
    action = ReviewAction(
        kind=ArtifactKind.DECISION_CARD,
        tool_name="decision_card_manage",
        payload={
            "id": "transfer-status-verification",
            "title": "Verify ambiguous transfer status",
            "scenario": "A bank transfer remains pending.",
            "signals": ["pending status", "duplicate submission pressure"],
            "principle": "Verify through official channels before resubmitting.",
            "outcome": "Avoided a duplicate transfer.",
            "boundaries": "Do not resubmit while the original status is pending.",
            "confidence": 0.91,
        },
        source_event_id="evt-card",
    )
    closed_loop, service = _coordinator(tmp_path, StaticBackend((action,)))
    service.ingest_conversation(
        _event("evt-card", "银行转账状态异常时先核验再决定是否重提。")
    )

    result = closed_loop.dream(IDS)

    paths = resolve_scope(tmp_path, IDS)
    assert result.status is PublicationStatus.ACTIVE
    assert (paths.decision_cards_dir / "transfer-status-verification.md").is_file()
    assert (
        "transfer-status-verification" in service.start_context(IDS)["decision_rules"]
    )


def test_auto_writeback_failure_restores_state_and_keeps_active_unchanged(
    tmp_path: Path,
) -> None:
    class FailingWriteback(DeterministicWritebackBackend):
        def render_user_persona(self, user_profile: str, limit: int) -> str:
            raise RuntimeError("writeback unavailable")

    _seed_decision_context(tmp_path)
    action = ReviewAction(
        kind=ArtifactKind.USER_PROFILE,
        tool_name="memory_manage",
        payload={
            "action": "add",
            "target": "user",
            "content": "User prefers structured output.",
            "confidence": 0.9,
        },
        source_event_id="evt-fail",
    )
    closed_loop, service = _coordinator(
        tmp_path,
        StaticBackend((action,)),
        writeback=FailingWriteback(),
    )
    service.ingest_conversation(_event("evt-fail", "以后使用结构化输出。"))

    with pytest.raises(ClosedLoopError):
        closed_loop.dream(IDS)

    assert closed_loop.status(IDS)["active"] is None
    assert closed_loop.status(IDS)["latest"].status is PublicationStatus.FAILED
    assert closed_loop.publications(IDS).pending_event_ids() == ("evt-fail",)
    profile = resolve_scope(tmp_path, IDS).user_root / "USER.md"
    assert not profile.exists()


def test_complete_report_writing_skill_auto_activates(tmp_path: Path) -> None:
    _seed_decision_context(tmp_path)
    _seed_user_context(tmp_path)
    action = ReviewAction(
        kind=ArtifactKind.SKILL,
        tool_name="skill_manage",
        payload={
            "id": "employee-report-writing",
            "title": "Employee Report Writing",
            "scenario": "Prepare a monthly employee report.",
            "inputs": ["approved activity records"],
            "steps": ["Collect", "Verify", "Render"],
            "output_template": "Summary, risks, next actions",
            "cautions": "Exclude unverified personal data.",
            "confidence": 0.9,
        },
        source_event_id="evt-skill",
    )
    closed_loop, service = _coordinator(tmp_path, StaticBackend((action,)))
    service.ingest_conversation(_event("evt-skill", "请沉淀员工月报的标准写作流程。"))

    result = closed_loop.dream(IDS)

    skill = resolve_scope(tmp_path, IDS).skills_dir / "employee-report-writing.skill"
    assert result.status is PublicationStatus.ACTIVE
    assert skill.is_file()
    assert "Summary, risks, next actions" in skill.read_text(encoding="utf-8")


def test_observed_preference_is_reinforced_by_a_later_dream(tmp_path: Path) -> None:
    _seed_decision_context(tmp_path)
    closed_loop, service = _coordinator(tmp_path, RepeatedPreferenceBackend())
    service.ingest_conversation(_event("evt-observe-1", "今天请把立即动作单独列出来。"))

    first = closed_loop.dream(IDS)

    assert first.status is PublicationStatus.ACTIVE
    assert "immediate-action section" not in service.start_context(IDS)["user_profile"]
    candidate_file = (
        resolve_scope(tmp_path, IDS).agent_root
        / "governance/users/user-1/candidates.json"
    )
    assert candidate_file.is_file()

    service.ingest_conversation(
        _event("evt-observe-2", "以后也请把立即动作单独列出来。")
    )
    second = closed_loop.dream(IDS)

    assert second.status is PublicationStatus.ACTIVE
    profile = service.start_context(IDS)["user_profile"]
    assert "immediate-action section" in profile
    assert "evt-observe-1" in profile
    assert "evt-observe-2" in profile

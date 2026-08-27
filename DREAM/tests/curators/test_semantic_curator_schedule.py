from datetime import datetime, timezone
from pathlib import Path

from dream.memory.artifacts import AtomicArtifactStore
from dream.curators.backend import AICurationPlan, UserCurationPlan
from dream.core.events import TaskCompletedEvent
from dream.memory.managers.decision_cards import DecisionCardManager
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.core.scope import ScopeIds, resolve_scope
from dream.application.service import DreamService


IDS = ScopeIds("acme", "assistant", "alice")


def _ledger_event(
    event_id: str = "evt-ledger",
    completed_at: str = "2026-07-21T00:00:00+08:00",
) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=IDS,
        completed_at=completed_at,
        interrupted=False,
        tool_iterations=1,
        transcript=({"role": "user", "content": "completed"},),
        final_response="Done.",
        source_refs=(),
    )


def _decision(card_id: str, principle: str, event_id: str) -> ReviewAction:
    return ReviewAction(
        kind=ArtifactKind.DECISION_CARD,
        tool_name="decision_card_manage",
        payload={
            "id": card_id,
            "title": "验证风险",
            "scenario": "不可逆操作",
            "signals": ["不可逆"],
            "principle": principle,
            "outcome": "降低风险。",
            "boundaries": "低风险除外。",
            "confidence": 0.8,
        },
        source_event_id=event_id,
    )


def _prepare_content(home: Path) -> tuple[str, str]:
    paths = resolve_scope(home, IDS)
    cards = DecisionCardManager(paths)
    cards.apply(_decision("verify-risk-a", "先验证，再执行。", "evt-1"))
    cards.apply(_decision("verify-risk-b", "执行前确认关键信息。", "evt-2"))
    artifacts = AtomicArtifactStore(paths.agent_root)
    artifacts.write_text(
        Path("users/alice/USER.md"),
        "Prefers concise answers.\n<!-- dream-source: evt-1 -->\n"
        "§\nPrefers brief replies.\n<!-- dream-source: evt-2 -->\n",
    )
    artifacts.write_text(Path("DECISION_RULES.md"), "# Active rules\n\n- unchanged\n")
    return (
        artifacts.read_text(Path("DECISION_RULES.md")),
        artifacts.read_text(Path("users/alice/USER.md")),
    )


class RecordingSemanticBackend:
    def __init__(self, *, fail: bool = False) -> None:
        self.ai_calls = 0
        self.user_calls = 0
        self.fail = fail

    def curate_ai(self, *, cards, current_rules):
        self.ai_calls += 1
        if self.fail:
            raise RuntimeError("semantic provider unavailable")
        return AICurationPlan(
            decision_rules_markdown=(
                "# Semantic candidate rules\n\n- 验证和确认先于不可逆执行。\n"
                "  - Evidence cards: verify-risk-a, verify-risk-b\n"
            ),
            archive_card_ids=("verify-risk-b",),
            summary="semantic AI merge",
        )

    def curate_user(self, profile):
        self.user_calls += 1
        if self.fail:
            raise RuntimeError("semantic provider unavailable")
        return UserCurationPlan(
            user_profile_markdown=(
                "Prefers concise replies.\n<!-- dream-sources: evt-1, evt-2 -->\n"
            ),
            summary="semantic user merge",
        )


def _service(
    home: Path,
    backend: RecordingSemanticBackend | None,
    *,
    enabled: bool,
) -> DreamService:
    service = DreamService(
        home,
        semantic_curator_backend=backend,
        semantic_curator_enabled=enabled,
        semantic_curator_interval_hours=168,
        semantic_curator_min_idle_hours=2,
    )
    service.ledger.append(_ledger_event())
    return service


def test_semantic_curator_is_disabled_by_default(tmp_path: Path) -> None:
    backend = RecordingSemanticBackend()
    active_before = _prepare_content(tmp_path)
    service = _service(tmp_path, backend, enabled=False)

    result = service.run_due_semantic_curators(
        datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc)
    )

    assert result == {}
    assert backend.ai_calls == 0
    assert backend.user_calls == 0
    paths = resolve_scope(tmp_path, IDS)
    artifacts = AtomicArtifactStore(paths.agent_root)
    assert artifacts.read_text(Path("DECISION_RULES.md")) == active_before[0]
    assert artifacts.read_text(Path("users/alice/USER.md")) == active_before[1]


def test_semantic_curator_requires_two_idle_hours_and_writes_isolated_candidate(
    tmp_path: Path,
) -> None:
    backend = RecordingSemanticBackend()
    active_before = _prepare_content(tmp_path)
    service = _service(tmp_path, backend, enabled=True)

    early = service.run_due_semantic_curators(
        datetime(2026, 7, 20, 17, 59, tzinfo=timezone.utc)
    )
    due = service.run_due_semantic_curators(
        datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)
    )

    assert early == {}
    assert backend.ai_calls == 1
    assert backend.user_calls == 1
    report = due["acme/assistant"]
    assert report["status"] == "candidate_ready"
    candidate = Path(str(report["candidate_path"]))
    assert candidate.exists()
    assert "Semantic candidate rules" in (candidate / "DECISION_RULES.md").read_text(
        encoding="utf-8"
    )
    paths = resolve_scope(tmp_path, IDS)
    artifacts = AtomicArtifactStore(paths.agent_root)
    assert artifacts.read_text(Path("DECISION_RULES.md")) == active_before[0]
    assert artifacts.read_text(Path("users/alice/USER.md")) == active_before[1]


def test_semantic_curator_runs_at_most_once_per_seven_day_interval(
    tmp_path: Path,
) -> None:
    backend = RecordingSemanticBackend()
    _prepare_content(tmp_path)
    service = _service(tmp_path, backend, enabled=True)

    first = service.run_due_semantic_curators(
        datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
    )
    too_soon = service.run_due_semantic_curators(
        datetime(2026, 7, 27, 23, 59, tzinfo=timezone.utc)
    )
    next_period = service.run_due_semantic_curators(
        datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)
    )

    assert first
    assert too_soon == {}
    assert next_period
    assert backend.ai_calls == 2


def test_semantic_failure_keeps_active_and_discards_candidate(tmp_path: Path) -> None:
    backend = RecordingSemanticBackend(fail=True)
    active_before = _prepare_content(tmp_path)
    service = _service(tmp_path, backend, enabled=True)

    result = service.run_due_semantic_curators(
        datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
    )

    report = result["acme/assistant"]
    assert report["status"] == "failed"
    assert report["candidate_path"] == ""
    paths = resolve_scope(tmp_path, IDS)
    artifacts = AtomicArtifactStore(paths.agent_root)
    assert artifacts.read_text(Path("DECISION_RULES.md")) == active_before[0]
    assert artifacts.read_text(Path("users/alice/USER.md")) == active_before[1]
    candidate_root = paths.agent_root / "semantic-curator-candidates"
    assert not candidate_root.exists() or not any(candidate_root.iterdir())


def test_pending_normal_dream_blocks_semantic_curator(tmp_path: Path) -> None:
    backend = RecordingSemanticBackend()
    _prepare_content(tmp_path)
    service = DreamService(
        tmp_path,
        semantic_curator_backend=backend,
        semantic_curator_enabled=True,
    )
    service.ingest_conversation(_ledger_event())

    result = service.run_due_semantic_curators(
        datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
    )

    assert result == {}
    assert backend.ai_calls == 0
